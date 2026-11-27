"""URL inputs and the SSRF guard (issue #35).

The address rules run against a stub resolver (no DNS) and a recording network backend, so
every case is deterministic: a refused address must never reach `connect_tcp`. Bodies,
redirects, timeouts and size caps run against real local HTTP listeners; "public" hosts are
stub names whose public address the recording backend routes to the local listener.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import ipaddress
import time

import httpcore
import pytest
from fastmcp import Client, FastMCP

from src.domain.fetch import (
    AddressPolicy,
    FetchSettings,
    UrlInputError,
    UrlRefusedError,
    address_problem,
    fetch_url,
    parse_target,
    redirect_target,
    set_fetch_settings,
    system_resolver,
)

PUBLIC = "93.184.216.34"
PUBLIC2 = "203.0.114.7"  # not 203.0.113/24 (documentation range, not global)
CAP = 1024 * 1024


# ----------------------------------------------------------------------------- helpers

class Resolver:
    """A stub resolver: host -> list of addresses (a list of lists = successive answers)."""

    def __init__(self, table: dict[str, list]):
        self.table = table
        self.calls: list[str] = []

    async def __call__(self, host: str, port: int):
        self.calls.append(host)
        answer = self.table[host]
        if answer and isinstance(answer[0], list):
            answer = answer[min(len(self.calls_for(host)), len(answer)) - 1]
        return [ipaddress.ip_address(a) for a in answer]

    def calls_for(self, host):
        return [h for h in self.calls if h == host]


class Backend(httpcore.AnyIOBackend):
    """Records the address every connection goes to and sends it to a local listener."""

    def __init__(self, routes: dict[str, int] | None = None):
        self.routes = routes or {}
        self.connected: list[str] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected.append(host)
        if host not in self.routes:
            raise httpcore.ConnectError(f"no route to {host}")
        return await super().connect_tcp("127.0.0.1", self.routes[host], timeout=timeout)


class Server:
    """A local HTTP listener; `handler(path, writer)` writes the raw response."""

    def __init__(self, handler):
        self.handler = handler
        self.paths: list[str] = []

    async def __aenter__(self):
        async def serve(reader, writer):
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                path = head.split(b" ", 2)[1].decode()
                self.paths.append(path)
                await self.handler(path, writer)
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                try:
                    writer.close()
                except Exception:
                    pass
        self.server = await asyncio.start_server(serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()


def response(body: bytes, status: str = "200 OK", headers: dict | None = None,
             length: int | None = -1) -> bytes:
    head = [f"HTTP/1.1 {status}"]
    for k, v in (headers or {}).items():
        head.append(f"{k}: {v}")
    if length == -1:
        head.append(f"Content-Length: {len(body)}")
    elif length is not None:
        head.append(f"Content-Length: {length}")
    head.append("Connection: close")
    return ("\r\n".join(head) + "\r\n\r\n").encode() + body


def static(body: bytes, **kw):
    async def handler(path, writer):
        writer.write(response(body, **kw))
        await writer.drain()
    return handler


def settings(allow=None, deny=None, **kw) -> FetchSettings:
    return FetchSettings(policy=AddressPolicy.from_lists(allow, deny), **kw)


async def refused(url, resolver, backend=None, s=None):
    backend = backend or Backend()
    with pytest.raises(UrlRefusedError) as e:
        await fetch_url(url, settings=s or settings(), max_bytes=CAP, resolver=resolver,
                        inner_backend=backend)
    assert backend.connected == [], "a refused address was connected to"
    return e.value


# ------------------------------------------------------------------ the address matrix

@pytest.mark.parametrize("address", [
    "127.0.0.1", "127.8.9.10", "::1", "0.0.0.0", "::", "10.0.0.1", "172.16.0.1", "172.31.255.1",
    "192.168.1.1", "100.64.0.1", "169.254.169.254", "fd00:ec2::254", "fe80::1", "fec0::1",
    "::ffff:127.0.0.1", "::ffff:10.1.2.3", "::127.0.0.1", "::ffff:0:7f00:1", "64:ff9b::7f00:1",
    "64:ff9b::a00:1", "64:ff9b:1::1", "2002:7f00:1::", "2002:a9fe:a9fe::", "2001::1",
    "192.0.0.9", "198.18.0.1", "224.0.0.1", "239.1.1.1", "240.0.0.1", "255.255.255.255",
    "ff02::1", "2001:db8::1", "192.0.2.1",
])
async def test_non_public_addresses_are_refused(address):
    """TC-URL-001: every non-public address (and IPv6 forms carrying one) is refused before
    any connection."""
    r = Resolver({"h.test": [address]})
    err = await refused("http://h.test/a.pdf", r)
    assert err.error_code == "url_refused"
    assert address_problem(ipaddress.ip_address(address)) is not None


@pytest.mark.parametrize("address", [PUBLIC, "8.8.8.8", "2606:4700:4700::1111",
                                     "::ffff:8.8.8.8", "64:ff9b::808:808", "2002:808:808::"])
def test_public_addresses_pass(address):
    """TC-URL-002: public addresses (and IPv6 forms carrying a public IPv4) are not refused."""
    assert address_problem(ipaddress.ip_address(address)) is None


@pytest.mark.parametrize("host", ["localhost", "2130706433", "0177.0.0.1", "0x7f000001",
                                  "0x7f.1", "127.1", "[::1]", "[::ffff:127.0.0.1]", "0"])
async def test_odd_host_forms_resolve_to_loopback_and_are_refused(host):
    """TC-URL-003: integer, octal, hex and short host forms are normalised by the real
    resolver to the address they mean, and refused."""
    await refused(f"http://{host}:9/x", system_resolver)


async def test_a_name_with_a_public_and_a_private_address_is_refused():
    """TC-URL-004: every resolved address must be public; one private address refuses the host."""
    await refused("http://mixed.test/", Resolver({"mixed.test": [PUBLIC, "10.0.0.5"]}))
    await refused("http://mixed.test/", Resolver({"mixed.test": ["10.0.0.5", PUBLIC]}))


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "gopher://h.test/", "ftp://h.test/a.pdf", "data:text/plain,hi",
    "dict://h.test:11211/", "h.test/a.pdf", "//h.test/a.pdf", "", "   ",
    "http://user:pass@h.test/a.pdf", "http://user@h.test/a.pdf", "https://:pw@h.test/",
    "http:///a.pdf", "http://h.test\r\nX: y/", "http://h.test:99999/",
])
async def test_bad_urls_are_refused_before_resolving(url):
    """TC-URL-005: only http/https, no userinfo, a host and a valid port."""
    r = Resolver({"h.test": [PUBLIC]})
    await refused(url, r)
    assert r.calls == []


# ----------------------------------------------------------------- pinning and rebinding

async def test_the_connection_goes_to_the_checked_address_dns_rebinding():
    """TC-URL-006: the name is resolved once per connection and the connection goes to the
    address that was checked; a second answer (rebinding to 127.0.0.1) is never asked for."""
    async with Server(static(b"%PDF-1.4 hello", headers={"Content-Type": "application/pdf"})) as srv:
        r = Resolver({"rebind.test": [[PUBLIC], ["127.0.0.1"]]})
        backend = Backend({PUBLIC: srv.port})
        got = await fetch_url("http://rebind.test/doc.pdf?sig=abc", settings=settings(),
                              max_bytes=CAP, resolver=r, inner_backend=backend)
    assert got.data == b"%PDF-1.4 hello"
    assert got.name == "doc.pdf" and got.mime == "application/pdf"
    assert backend.connected == [PUBLIC]
    assert r.calls == ["rebind.test"]


async def test_a_rebinding_name_is_refused_when_its_first_answer_is_private():
    """TC-URL-007: the address checked is the address used: private first answer, refused."""
    await refused("http://rebind.test/", Resolver({"rebind.test": [["127.0.0.1"], [PUBLIC]]}))


# ---------------------------------------------------------------------------- redirects

def redirecting(location: str):
    async def handler(path, writer):
        writer.write(response(b"", status="302 Found", headers={"Location": location}))
        await writer.drain()
    return handler


async def test_redirect_to_a_private_address_is_refused():
    """TC-URL-008: each redirect hop is checked again: public -> 10.0.0.5 is refused."""
    async with Server(redirecting("http://internal.test/secret")) as srv:
        r = Resolver({"pub.test": [PUBLIC], "internal.test": ["10.0.0.5"]})
        backend = Backend({PUBLIC: srv.port, "10.0.0.5": srv.port})
        with pytest.raises(UrlRefusedError):
            await fetch_url("http://pub.test/", settings=settings(), max_bytes=CAP,
                            resolver=r, inner_backend=backend)
    assert backend.connected == [PUBLIC]
    assert srv.paths == ["/"]


async def test_redirect_to_a_literal_loopback_address_is_refused():
    """TC-URL-009: a redirect to http://127.0.0.1/ is refused like a first URL."""
    async with Server(redirecting("http://127.0.0.1:1/")) as srv:
        backend = Backend({PUBLIC: srv.port})
        with pytest.raises(UrlRefusedError):
            await fetch_url("http://pub.test/", settings=settings(), max_bytes=CAP,
                            resolver=Resolver({"pub.test": [PUBLIC], "127.0.0.1": ["127.0.0.1"]}),
                            inner_backend=backend)
    assert backend.connected == [PUBLIC]


async def test_redirects_are_followed_up_to_five_times():
    """TC-URL-010: 5 redirects are followed (relative locations too); a 6th is refused."""
    async def chain(path, writer):
        n = int(path.strip("/") or 0)
        if n < hops:
            writer.write(response(b"", status="301 Moved", headers={"Location": f"/{n + 1}"}))
        else:
            writer.write(response(b"done"))
        await writer.drain()

    for hops, ok in ((5, True), (6, False)):
        async with Server(chain) as srv:
            backend = Backend({PUBLIC: srv.port})
            call = fetch_url("http://pub.test/", settings=settings(), max_bytes=CAP,
                             resolver=Resolver({"pub.test": [PUBLIC]}), inner_backend=backend)
            if ok:
                assert (await call).data == b"done"
            else:
                with pytest.raises(UrlRefusedError, match="more than 5"):
                    await call
            assert len(srv.paths) == min(hops, 5) + 1


def test_https_to_http_redirect_is_refused():
    """TC-URL-011: a redirect from https to http is not followed; http -> https is."""
    with pytest.raises(UrlRefusedError, match="https to http"):
        redirect_target(parse_target("https://a.test/x"), "http://a.test/y")
    assert redirect_target(parse_target("http://a.test/x"), "https://b.test/y").scheme == "https"
    assert redirect_target(parse_target("https://a.test/d/x"), "y?q=1").target == "/d/y?q=1"
    with pytest.raises(UrlRefusedError):
        redirect_target(parse_target("https://a.test/x"), "file:///etc/passwd")


# ------------------------------------------------------------------------------ size caps

async def fetch_public(handler, max_bytes=CAP, s=None):
    async with Server(handler) as srv:
        return await fetch_url("http://pub.test/f.bin", settings=s or settings(),
                               max_bytes=max_bytes, resolver=Resolver({"pub.test": [PUBLIC]}),
                               inner_backend=Backend({PUBLIC: srv.port}))


async def test_content_length_over_the_cap_is_refused_before_reading():
    """TC-URL-012: a declared length over the cap is refused."""
    async def handler(path, writer):
        writer.write(response(b"x" * 10, length=CAP + 1))
        await writer.drain()
    with pytest.raises(UrlInputError) as e:
        await fetch_public(handler)
    assert e.value.error_code == "url_too_large"


async def test_a_body_at_the_cap_is_accepted_and_one_byte_more_is_not():
    """TC-URL-013: the cap is exact, without a Content-Length (read until close)."""
    assert len((await fetch_public(static(b"a" * CAP, length=None))).data) == CAP
    with pytest.raises(UrlInputError, match="too large"):
        await fetch_public(static(b"a" * (CAP + 1), length=None))


async def test_a_lying_content_length_never_yields_more_than_it_declared():
    """TC-URL-014: a short Content-Length followed by a longer body: only the declared bytes
    are read, so the cap cannot be passed this way."""
    got = await fetch_public(static(b"b" * (3 * CAP), length=100))
    assert got.data == b"b" * 100


async def test_a_chunked_body_over_the_cap_is_refused():
    """TC-URL-015: chunked transfer is counted while it streams."""
    async def handler(path, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
        for _ in range(40):
            chunk = b"c" * 65536
            writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            await writer.drain()
        writer.write(b"0\r\n\r\n")
        await writer.drain()
    with pytest.raises(UrlInputError, match="too large"):
        await fetch_public(handler)


async def test_a_gzip_bomb_stops_at_the_cap():
    """TC-URL-016: the cap counts decompressed bytes: 200 MB of zeros in ~200 KB of gzip is
    refused at the 1 MB cap, quickly."""
    bomb = gzip.compress(b"\0" * (200 * 1024 * 1024), compresslevel=9)
    assert len(bomb) < CAP
    started = time.monotonic()
    with pytest.raises(UrlInputError, match="too large"):
        await fetch_public(static(bomb, headers={"Content-Encoding": "gzip"}))
    assert time.monotonic() - started < 5


async def test_gzip_and_deflate_bodies_are_decoded():
    """TC-URL-017: gzip and deflate (zlib and raw) are decoded; other codings are refused."""
    import zlib
    data = b"hello world " * 1000
    assert (await fetch_public(static(gzip.compress(data),
                                      headers={"Content-Encoding": "gzip"}))).data == data
    assert (await fetch_public(static(zlib.compress(data),
                                      headers={"Content-Encoding": "deflate"}))).data == data
    raw = zlib.compressobj(wbits=-15)
    raw_body = raw.compress(data) + raw.flush()
    assert (await fetch_public(static(raw_body,
                                      headers={"Content-Encoding": "deflate"}))).data == data
    with pytest.raises(UrlInputError, match="content coding"):
        await fetch_public(static(b"x", headers={"Content-Encoding": "br"}))


async def test_http_errors_are_reported():
    """TC-URL-018: a non-200 answer is an error naming the status."""
    with pytest.raises(UrlInputError, match="HTTP 404"):
        await fetch_public(static(b"nope", status="404 Not Found"))


# ------------------------------------------------------------------------------- timeouts

async def test_a_server_that_stops_sending_hits_the_idle_timeout():
    """TC-URL-019: no byte for the idle limit stops the fetch."""
    async def stall(path, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nabc")
        await writer.drain()
        await asyncio.sleep(5)
    started = time.monotonic()
    with pytest.raises(UrlInputError, match="stopped sending"):
        await fetch_public(stall, s=settings(idle_timeout_sec=0.5, total_timeout_sec=10))
    assert time.monotonic() - started < 3


async def test_a_slow_drip_hits_the_total_timeout():
    """TC-URL-020: a byte every 0.2 s never trips the idle limit, the total limit stops it."""
    async def drip(path, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
        for _ in range(1000):
            writer.write(b"d")
            await writer.drain()
            await asyncio.sleep(0.2)
    started = time.monotonic()
    with pytest.raises(UrlInputError, match="longer than 1.5 s"):
        await fetch_public(drip, s=settings(idle_timeout_sec=1, total_timeout_sec=1.5))
    assert time.monotonic() - started < 4


# --------------------------------------------------------------------------- allow / deny

async def test_allowlisted_lan_network_and_host_are_fetched():
    """TC-URL-021: MORPHYARD_URL_ALLOW lets a LAN network or an exact host name through."""
    async with Server(static(b"lan file")) as srv:
        for allow, host, addr in (("192.168.1.0/24", "nas.test", "192.168.1.20"),
                                  ("nas.lan", "nas.lan", "10.9.9.9")):
            got = await fetch_url(f"http://{host}/f.txt", settings=settings(allow=allow),
                                  max_bytes=CAP, resolver=Resolver({host: [addr]}),
                                  inner_backend=Backend({addr: srv.port}))
            assert got.data == b"lan file"
    await refused("http://other.lan/", Resolver({"other.lan": ["192.168.2.1"]}),
                  s=settings(allow="192.168.1.0/24,nas.lan"))


async def test_deny_wins_over_allow_and_can_deny_public_hosts():
    """TC-URL-022: MORPHYARD_URL_DENY refuses networks and hosts, also allowed or public ones."""
    s = settings(allow="192.168.1.0/24,nas.lan", deny="192.168.1.20/32,nas.lan,93.184.0.0/16")
    await refused("http://nas.test/", Resolver({"nas.test": ["192.168.1.20"]}), s=s)
    await refused("http://nas.lan/", Resolver({"nas.lan": ["192.168.1.30"]}), s=s)
    await refused("http://pub.test/", Resolver({"pub.test": [PUBLIC]}), s=s)
    await refused("http://pub.test/", Resolver({"pub.test": ["::ffff:" + PUBLIC]}), s=s)


async def test_unspecified_and_multicast_are_never_fetched_even_when_allowed():
    """TC-URL-023: allowing 0.0.0.0/0 does not open 0.0.0.0 or multicast."""
    s = settings(allow="0.0.0.0/0,::/0")
    for addr in ("0.0.0.0", "224.0.0.1", "::", "ff02::1"):
        await refused("http://h.test/", Resolver({"h.test": [addr]}), s=s)


def test_bad_allow_or_deny_entries_stop_startup():
    """TC-URL-024: an entry that is neither a CIDR nor a host name is a config error."""
    from pydantic import ValidationError

    from src.config.model import FetchConfig
    cfg = FetchConfig(allow="192.168.1.0/24, nas.lan ,", deny="")
    assert cfg.allow == ["192.168.1.0/24", "nas.lan"] and cfg.deny == []
    for bad in ("http://nas.lan", "192.168.1.0/33", "nas lan", "a..b"):
        with pytest.raises(ValidationError):
            FetchConfig(allow=bad)


async def test_url_input_can_be_turned_off():
    """TC-URL-025: MORPHYARD_URL_FETCH=false refuses every URL."""
    await refused("http://pub.test/", Resolver({"pub.test": [PUBLIC]}),
                  s=FetchSettings(enabled=False))


# ------------------------------------------------------------------------------ own links

@pytest.fixture
def store_runtime(tmp_path):
    from src.domain import files
    from src.domain.files import FilesRuntime, LinkSigner, LocalStore
    store = LocalStore(tmp_path / "store", default_ttl_sec=600, max_total_bytes=10 * CAP,
                       max_owner_bytes=10 * CAP)
    signer = LinkSigner(b"k" * 32, "http://127.0.0.1:5066")
    files.set_runtime(FilesRuntime(store=store, signer=signer, sweep_interval_sec=60,
                                   result_delivery="auto", inline_max_bytes=CAP))
    return store, signer


async def test_own_download_links_are_read_from_the_store_without_a_fetch(store_runtime):
    """TC-URL-026: a link of this server is served from the store (no resolve, no connect);
    a tampered or expired one is refused."""
    store, signer = store_runtime
    meta = store.put_bytes("someone", b"%PDF-1.4 own", name="out.pdf", mime="application/pdf")
    link = signer.url(meta.file_id, meta.name, time.time() + 300)
    r, backend = Resolver({}), Backend()
    got = await fetch_url(link, settings=settings(), max_bytes=CAP, resolver=r,
                          inner_backend=backend)
    assert (got.data, got.name, got.mime) == (b"%PDF-1.4 own", "out.pdf", "application/pdf")
    assert r.calls == [] and backend.connected == []
    for bad in (link.replace("sig=", "sig=A"), link.replace("out.pdf", "other.pdf"),
                signer.url(meta.file_id, meta.name, time.time() - 1)):
        with pytest.raises(UrlRefusedError, match="not valid or has expired"):
            await fetch_url(bad, settings=settings(), max_bytes=CAP, resolver=r,
                            inner_backend=backend)
    assert r.calls == [] and backend.connected == []


# ---------------------------------------------------------------------------------- logs

async def test_logs_never_contain_the_path_or_query(caplog_loguru):
    """TC-URL-027: logs name scheme, host and port only, for fetched and refused URLs."""
    async with Server(static(b"x")) as srv:
        await fetch_url("http://pub.test/private-path/f.pdf?X-Amz-Signature=SECRET123",
                        settings=settings(), max_bytes=CAP,
                        resolver=Resolver({"pub.test": [PUBLIC]}),
                        inner_backend=Backend({PUBLIC: srv.port}))
    with pytest.raises(UrlRefusedError):
        await fetch_url("http://10.0.0.1/p2/f.pdf?token=SECRET456", settings=settings(),
                        max_bytes=CAP, resolver=Resolver({"10.0.0.1": ["10.0.0.1"]}))
    text = "\n".join(caplog_loguru)
    assert "pub.test" in text and "10.0.0.1" in text
    for secret in ("SECRET123", "SECRET456", "private-path", "p2/"):
        assert secret not in text


# ------------------------------------------------------------------------------ the tools

@pytest.fixture
def local_allowed():
    set_fetch_settings(settings(allow="127.0.0.1/32"))
    yield
    set_fetch_settings(FetchSettings())


async def _call(tool: str, args: dict):
    mcp = FastMCP("t")
    from src.fastmcp_tools.morphyard_tools import register_morphyard_tools
    register_morphyard_tools(mcp)
    async with Client(mcp) as client:
        return await client.call_tool(tool, args, raise_on_error=False)


async def test_tools_take_a_url(local_allowed):
    """TC-URL-028: a tool fetches its input from `url`; name and type come from the server."""
    md = b"# Title\n\nSome text."
    async with Server(static(md, headers={"Content-Disposition": 'attachment; filename="notes.md"'})) as srv:
        res = await _call("extract_text", {"url": f"http://127.0.0.1:{srv.port}/dl?id=1"})
    assert not res.is_error, res.content
    assert "Some text." in res.content[0].text


async def test_tools_refuse_a_private_url_by_default():
    """TC-URL-029: without an allow list, a loopback URL is a tool error naming the rule."""
    res = await _call("extract_text", {"url": "http://127.0.0.1:9/a.md"})
    assert res.is_error and "only fetches public addresses" in res.content[0].text


async def test_only_one_input_may_be_given():
    """TC-URL-030: url together with file_content or file_id is refused before any fetch."""
    b64 = base64.b64encode(b"# x").decode()
    for extra in ({"file_content": b64, "file_name": "a.md"}, {"file_id": "f_abc"}):
        res = await _call("extract_text", {"url": "http://127.0.0.1:9/a.md", **extra})
        assert res.is_error and "only one of file_content, file_id and url" in res.content[0].text


async def test_pdf_merge_items_take_a_url(local_allowed):
    """TC-URL-031: pdf_merge items accept `url` like the single-file tools."""
    from tests.test_pdf_pages import make_pdf  # noqa: PLC0415
    pdf = make_pdf(2)
    async with Server(static(pdf, headers={"Content-Type": "application/pdf"})) as srv:
        res = await _call("pdf_merge", {"files": [
            {"url": f"http://127.0.0.1:{srv.port}/one.pdf"},
            {"file_content": base64.b64encode(pdf).decode(), "file_name": "two.pdf"}]})
    assert not res.is_error, res.content
    assert "4 page" in res.content[0].text


def test_the_decoder_never_produces_more_than_the_room_left():
    """TC-URL-032: one compressed chunk of a bomb is decoded to at most room + 1 bytes, so
    memory stays bounded by the cap, not by the compression ratio."""
    from src.domain.fetch.fetcher import _Decoder
    bomb = gzip.compress(b"\0" * (50 * 1024 * 1024))
    out = _Decoder("gzip").feed(bomb[:65536], 1000)
    assert len(out) == 1001


@pytest.mark.parametrize("address, kind", [
    ("0.0.0.1", "the unspecified address"), ("127.0.0.1", "a loopback address"),
    ("10.1.1.1", "a private network address"), ("172.20.0.1", "a private network address"),
    ("192.168.0.1", "a private network address"), ("169.254.169.254", "cloud metadata"),
    ("100.64.0.1", "carrier-grade NAT"), ("192.0.0.9", "a special-purpose address"),
    ("198.18.0.1", "a special-purpose address"), ("224.0.0.1", "a multicast address"),
    ("240.0.0.1", "a reserved address"), ("::", "the unspecified address"),
    ("::1", "a loopback address"), ("fd00::1", "a private network address"),
    ("fec0::1", "a private network address"), ("fe80::1", "a link-local address"),
    ("ff02::1", "a multicast address"), ("2001::1", "a Teredo tunnel address"),
    ("64:ff9b:1::1", "a local-use NAT64 address"),
    ("::ffff:10.0.0.1", "a private network address inside an IPv6 address"),
    ("2001:db8::1", "a reserved or non-public address"),
])
async def test_the_refusal_names_the_kind_of_address(address, kind):
    """TC-URL-033: the refusal says which kind of address the host resolved to, so the model
    can tell the user why (and an operator what to allow)."""
    err = await refused("http://h.test/", Resolver({"h.test": [address]}),
                        s=settings(allow="h.other"))
    assert kind in err.message


def test_request_argument_logging_cuts_urls_to_their_host():
    """TC-URL-034: the debug log of tool arguments keeps only scheme://host:port of a url
    (also inside pdf_merge items), never the path, query or credentials."""
    from src.middleware.request_logging import redact_url_args
    args = {"url": "https://u:pw@files.test:8443/a/b.pdf?X-Amz-Signature=SECRET",
            "files": [{"url": "http://h.test/x?token=SECRET2"}, {"file_id": "f_1"}],
            "text": "keep"}
    out = repr(redact_url_args(args))
    assert "SECRET" not in out and "pw" not in out and "/a/b.pdf" not in out
    assert "https://files.test:8443/…" in out and "http://h.test/…" in out
    assert "f_1" in out and "keep" in out and args["url"].endswith("SECRET")
