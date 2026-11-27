"""Fetch a URL input with the SSRF guard (issue #35).

What the guard does, in the order a fetch meets it:

  - **URL:** http and https only; no `user:password@` part; a host is required.
  - **Own links:** a download link of this server (`<public_url>/files/...`) is read from the
    store after its signature is checked, never fetched (the guard would rightly refuse to
    connect to the server's own, often loopback, address).
  - **Address, pinned:** the host is resolved once per connection, every address it resolves to
    is judged by `AddressPolicy`, and the connection is made to the address that was judged
    (`PinnedBackend`). Nothing resolves the name a second time, so DNS rebinding cannot swap in
    another address between the check and the connection. TLS still checks the certificate
    against the host name. No proxy is used, whatever the environment says.
  - **Redirects** are followed by hand, at most 5, and every hop goes through all of the above
    again; a redirect from https to http is refused.
  - **Size:** a `Content-Length` over the input limit is refused before the body is read; the
    body is counted while it streams, after decompression (gzip and deflate are decoded here,
    in steps that never produce more than the limit, so a decompression bomb stops at the cap);
    other content codings are refused.
  - **Time:** connect 10 s, 15 s without a byte, and a total for the whole fetch
    (`MORPHYARD_URL_TIMEOUT_SEC`, 60 s by default).
  - No cookies, no credentials, a fixed `User-Agent`. Logs name the scheme, host and port,
    never the path or query string (presigned URLs carry secrets there).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import time
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from email.message import Message
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import certifi
import httpcore

from src.domain.fetch.policy import (
    AddressPolicy,
    IPAddress,
    UrlInputError,
    UrlRefusedError,
    normalize_host,
)
from src.log import get_server_logger
from src.version import get_version

logger = get_server_logger()

MAX_REDIRECTS = 5
CONNECT_TIMEOUT_SEC = 10.0
IDLE_TIMEOUT_SEC = 15.0
_REDIRECTS = {301, 302, 303, 307, 308}
_CHUNK = 64 * 1024

Resolver = Callable[[str, int], Awaitable[list[IPAddress]]]


@dataclass(frozen=True)
class FetchSettings:
    """`fetch:` in config.yaml (MORPHYARD_URL_*)."""

    enabled: bool = True
    policy: AddressPolicy = field(default_factory=AddressPolicy)
    total_timeout_sec: float = 60.0
    connect_timeout_sec: float = CONNECT_TIMEOUT_SEC
    idle_timeout_sec: float = IDLE_TIMEOUT_SEC
    max_redirects: int = MAX_REDIRECTS


@dataclass(frozen=True)
class Fetched:
    data: bytes
    name: str | None
    mime: str | None


@dataclass(frozen=True)
class Target:
    """A URL that passed the URL-level checks."""

    url: str
    scheme: str
    host: str          # lower case, no brackets, IDNA-encoded
    port: int
    target: str        # path + query, as sent

    @property
    def where(self) -> str:
        """scheme://host:port, what logs and messages may show."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"


async def system_resolver(host: str, port: int) -> list[IPAddress]:
    """Every address `host` resolves to (numeric forms such as 2130706433 included)."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as e:
        raise UrlInputError(f"The host {host} could not be resolved ({e}).") from None
    seen: list[IPAddress] = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip not in seen:
            seen.append(ip)
    return seen


def parse_target(url: str) -> Target:
    """Check a URL's form (scheme, credentials, host); raises UrlRefusedError."""
    if not isinstance(url, str) or not url.strip():
        raise UrlRefusedError("The url is empty.")
    url = url.strip()
    if any(ch in url for ch in "\r\n\t") or any(ord(ch) < 32 for ch in url):
        raise UrlRefusedError("The url contains control characters.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UrlRefusedError("The url is not a valid http or https address.") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise UrlRefusedError(
            f"Only http and https URLs can be fetched, not {scheme or 'a URL without a scheme'}"
            f"{':' if scheme else ''}. Ask the user for the file itself.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UrlRefusedError(
            "URLs with a user name or password (user:password@host) are not fetched: "
            "credentials are never sent. Use a link that needs none, such as a share link.")
    host = parts.hostname or ""
    if not host:
        raise UrlRefusedError("The url has no host.")
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        host = normalize_host(host)
    except ValueError:
        try:
            host = normalize_host(host).encode("idna").decode("ascii")
        except UnicodeError:
            raise UrlRefusedError("The url's host name is not valid.") from None
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return Target(url=url, scheme=scheme, host=host,
                  port=port or (443 if scheme == "https" else 80), target=target)


def redirect_target(current: Target, location: str) -> Target:
    """The next hop of a redirect, checked like the first URL; https -> http is refused."""
    nxt = parse_target(urljoin(current.url, location.strip()))
    if current.scheme == "https" and nxt.scheme == "http":
        raise UrlRefusedError(
            f"{current.where} redirected to plain http ({nxt.where}); a redirect from https to "
            f"http is not followed.")
    return nxt


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """Resolves the host once, checks every address, connects to a checked address."""

    def __init__(self, policy: AddressPolicy, resolver: Resolver,
                 inner: httpcore.AsyncNetworkBackend | None = None):
        self._policy = policy
        self._resolver = resolver
        self._inner = inner or httpcore.AnyIOBackend()
        self.peers: list[IPAddress] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        addresses = await self._resolver(host, port)
        checked = self._policy.check(host, addresses)
        last: Exception | None = None
        for ip in checked:
            try:
                stream = await self._inner.connect_tcp(
                    str(ip), port, timeout=timeout, local_address=local_address,
                    socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as e:
                last = e
                continue
            self.peers.append(ip)
            return stream
        raise last if last is not None else UrlInputError(f"The host {host} has no address.")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise UrlRefusedError("Unix sockets are never fetched.")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


# ---------------------------------------------------------------------------------------- body

class _Decoder:
    """Content decoding that never produces more than it is asked for."""

    def __init__(self, coding: str):
        self._raw = coding in ("", "identity")
        # gzip, or deflate as zlib data (raw deflate is tried when the zlib header is wrong)
        self._coding = coding
        self._d = None if self._raw else zlib.decompressobj(
            16 + zlib.MAX_WBITS if coding == "gzip" else zlib.MAX_WBITS)
        self._first = True

    def feed(self, chunk: bytes, room: int) -> bytes:
        """Decoded bytes for `chunk`, at most `room` + 1 of them (enough to see the cap)."""
        if self._raw:
            return chunk
        if self._coding == "deflate" and self._first and chunk:
            self._first = False
            try:
                return self._d.decompress(chunk, room + 1)
            except zlib.error:
                self._d = zlib.decompressobj(-zlib.MAX_WBITS)
        return self._decompress(chunk, room)

    def _decompress(self, data: bytes, room: int) -> bytes:
        try:
            out = self._d.decompress(data, room + 1)
            # unconsumed input left means the limit stopped it: the cap is already reached
        except zlib.error as e:
            raise UrlInputError(f"The server sent data that does not decompress ({e}).") from None
        return out


def _content_coding(headers: list[tuple[bytes, bytes]]) -> str:
    codings = [v.decode("latin-1").strip().lower() for k, v in headers
               if k.lower() == b"content-encoding"]
    joined = ",".join(c for c in codings if c)
    parts = [p.strip() for p in joined.split(",") if p.strip() and p.strip() != "identity"]
    if not parts:
        return ""
    if len(parts) == 1 and parts[0] in ("gzip", "x-gzip", "deflate"):
        return "gzip" if parts[0] == "x-gzip" else parts[0]
    raise UrlInputError(f"The server sent the file with the content coding {joined!r}, which "
                        f"this server does not decode (it asks for gzip or deflate only).")


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for k, v in headers:
        if k.lower() == name:
            return v.decode("latin-1")
    return None


def _too_large(max_bytes: int, size: int | None = None) -> UrlInputError:
    shown = f"{size / 1024 / 1024:.1f} MB" if size is not None else "more than that"
    return UrlInputError(
        f"File too large: {shown}, limit is {max_bytes / 1024 / 1024:g} MB. Tell the user the "
        f"file exceeds the processing limit. Retrying with the same URL will not succeed.",
        "url_too_large")


def file_name_from(headers: list[tuple[bytes, bytes]], target: Target) -> str | None:
    """Content-Disposition's file name, else the last segment of the URL path."""
    disposition = _header(headers, b"content-disposition")
    name = None
    if disposition:
        msg = Message()
        msg["content-disposition"] = disposition
        try:
            name = msg.get_filename()
        except Exception:  # pragma: no cover - malformed header: fall back to the path
            name = None
    if not name:
        name = unquote(target.target.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1])
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip().strip(".") if name else ""
    name = "".join(ch for ch in name if ord(ch) >= 32)[:200]
    return name or None


# ---------------------------------------------------------------------------------- own links

def _own_link(target: Target) -> Fetched | None:
    """A download link of this server, read from the store; None for any other URL."""
    from src.domain.files import get_signer, get_store

    signer, store = get_signer(), get_store()
    if signer is None or store is None:
        return None
    try:
        own = parse_target(signer.public_url)
    except UrlRefusedError:
        return None
    if (own.scheme, own.host, own.port) != (target.scheme, target.host, target.port):
        return None
    prefix = own.target.split("?", 1)[0].rstrip("/") + "/files/"
    path, _, query = target.target.partition("?")
    if not path.startswith(prefix):
        return None
    parts = path[len(prefix):].split("/")
    invalid = UrlRefusedError(
        "This Morphyard download link is not valid or has expired. Run the tool that made the "
        "file again to get a new link (or pass its file_id).")
    if len(parts) != 2:
        raise invalid
    file_id, name = parts[0], unquote(parts[1])
    q = parse_qs(query)
    exp, sig = (q.get("exp") or [None])[0], (q.get("sig") or [None])[0]
    if not signer.verify(file_id, name, exp, sig):
        raise invalid
    meta = store.get(file_id)
    if meta is None or meta.name != name:
        raise invalid
    try:
        with store.open(file_id) as f:
            data = f.read()
    except FileNotFoundError:
        raise invalid from None
    logger.info(f"URL input: own download link {file_id[:10]}… read from the store "
                f"({len(data)} bytes)")
    return Fetched(data=data, name=meta.name, mime=meta.mime)


# --------------------------------------------------------------------------------------- fetch

def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


async def _read_body(response, max_bytes: int) -> bytes:
    decoder = _Decoder(_content_coding(response.headers))
    out = bytearray()
    async for chunk in response.aiter_stream():
        room = max_bytes - len(out)
        piece = decoder.feed(chunk, room)
        if len(piece) > room:
            raise _too_large(max_bytes)
        out += piece
    return bytes(out)


async def fetch_url(url: str, *, settings: FetchSettings, max_bytes: int,
                    resolver: Resolver = system_resolver,
                    inner_backend: httpcore.AsyncNetworkBackend | None = None) -> Fetched:
    """The file at `url`, through every check above; raises UrlInputError (or UrlRefusedError)."""
    if not settings.enabled:
        raise UrlRefusedError(
            "URL input is turned off on this server (MORPHYARD_URL_FETCH=false). Pass the file "
            "itself in file_content instead.")
    target = parse_target(url)
    first = target.where
    started = time.monotonic()
    try:
        async with asyncio.timeout(settings.total_timeout_sec):
            for _hop in range(settings.max_redirects + 1):
                own = _own_link(target)
                if own is not None:
                    return own
                status, result, location = await _get_once(
                    target, settings, max_bytes, resolver, inner_backend)
                if result is not None:
                    logger.info(f"URL input: {first} → {target.where} {status}, "
                                f"{len(result.data)} bytes in {time.monotonic() - started:.1f}s")
                    return result
                target = redirect_target(target, location)
            raise UrlRefusedError(f"{first} redirected more than {settings.max_redirects} times; "
                                  f"the redirects were not followed further.")
    except TimeoutError:
        logger.info(f"URL input: {first} refused: no file after {settings.total_timeout_sec:g}s")
        raise UrlInputError(
            f"Fetching from {first} took longer than {settings.total_timeout_sec:g} s and was "
            f"stopped. Ask the user for the file itself.") from None
    except UrlInputError as e:
        logger.info(f"URL input: {first} refused ({e.error_code}) via {target.where}")
        raise


async def _get_once(target: Target, settings: FetchSettings, max_bytes: int,
                    resolver: Resolver, inner_backend) -> tuple[int, Fetched | None, str | None]:
    """One GET: (status, the file, None) or (status, None, redirect location)."""
    backend = PinnedBackend(settings.policy, resolver, inner_backend)
    host_header = f"[{target.host}]" if ":" in target.host else target.host
    if target.port != (443 if target.scheme == "https" else 80):
        host_header += f":{target.port}"
    url = httpcore.URL(scheme=target.scheme.encode(), host=target.host.encode("ascii"),
                       port=target.port, target=target.target.encode("utf-8", "surrogateescape"))
    headers = [
        (b"Host", host_header.encode("ascii")),
        (b"User-Agent", f"Morphyard/{get_version()}".encode("ascii")),
        (b"Accept", b"*/*"),
        (b"Accept-Encoding", b"gzip, deflate"),
        (b"Connection", b"close"),
    ]
    timeouts = {"connect": settings.connect_timeout_sec, "read": settings.idle_timeout_sec,
                "write": settings.idle_timeout_sec, "pool": settings.connect_timeout_sec}
    try:
        async with httpcore.AsyncConnectionPool(
                ssl_context=_ssl_context(), max_connections=1, retries=0,
                network_backend=backend) as pool:
            async with pool.stream("GET", url, headers=headers,
                                   extensions={"timeout": timeouts}) as response:
                status = response.status
                if status in _REDIRECTS:
                    location = _header(response.headers, b"location")
                    if not location:
                        raise UrlInputError(f"{target.where} answered {status} without a Location.")
                    return status, None, location
                if status != 200:
                    raise UrlInputError(
                        f"{target.where} answered HTTP {status}; no file was fetched. Check the "
                        f"link with the user.")
                length = _header(response.headers, b"content-length")
                # The coded length: decoding only makes the file larger, so it is refused too.
                if length is not None and length.strip().isdigit() and int(length) > max_bytes:
                    raise _too_large(max_bytes, int(length))
                data = await _read_body(response, max_bytes)
                ctype = _header(response.headers, b"content-type")
                mime = ctype.split(";", 1)[0].strip().lower() if ctype else None
                return status, Fetched(data=data, name=file_name_from(response.headers, target),
                                       mime=mime or None), None
    except httpcore.ReadTimeout:
        raise UrlInputError(f"{target.where} stopped sending for {settings.idle_timeout_sec:g} s; "
                            f"the fetch was stopped.") from None
    except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
        raise UrlInputError(f"Could not connect to {target.where} ({type(e).__name__}).") from None
    except httpcore.TimeoutException as e:
        raise UrlInputError(f"{target.where} timed out ({type(e).__name__}).") from None
    except ssl.SSLError as e:
        raise UrlInputError(f"The TLS connection to {target.where} failed ({e.reason or e}).") from None
    except (httpcore.ProtocolError, httpcore.NetworkError) as e:
        raise UrlInputError(f"The connection to {target.where} failed ({type(e).__name__}).") from None


__all__ = [
    "FetchSettings", "Fetched", "PinnedBackend", "Target", "fetch_url", "file_name_from",
    "parse_target", "redirect_target", "system_resolver",
]
