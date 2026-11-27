"""Which addresses a URL input may reach (issue #35).

By default only public (globally routable unicast) addresses are fetched. Everything a server
can reach that the caller could not - loopback, private networks, link-local (cloud metadata at
169.254.169.254), carrier-grade NAT, benchmark and reserved ranges, multicast, the unspecified
address - is refused. IPv6 addresses that carry an IPv4 address (IPv4-mapped `::ffff:a.b.c.d`,
IPv4-compatible `::a.b.c.d`, IPv4-translated `::ffff:0:a.b.c.d`, 6to4 `2002::/16` and NAT64
`64:ff9b::/96`) are judged by the IPv4 address inside; Teredo and local-use NAT64 are refused.

A host name is judged by **every** address it resolves to: one non-public address refuses the
whole name, so a name that resolves to a public and a private address cannot be used to reach the
private one.

`MORPHYARD_URL_ALLOW` lists networks (CIDR) and exact host names that may be reached although they
are not public (a NAS on the LAN). `MORPHYARD_URL_DENY` lists networks and host names that are
always refused; deny wins over allow. The unspecified address and multicast are never fetched.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from src.domain.exceptions import DomainException

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class UrlInputError(DomainException):
    """A URL input that was refused or could not be fetched (the message says which)."""

    def __init__(self, message: str, error_code: str = "url_fetch_failed"):
        super().__init__(message, error_code)


class UrlRefusedError(UrlInputError):
    """A URL this server will not fetch (scheme, credentials, address)."""

    def __init__(self, message: str):
        super().__init__(message, "url_refused")


def _nets(*cidrs: str) -> tuple[IPNetwork, ...]:
    return tuple(ipaddress.ip_network(c) for c in cidrs)


# Refused on top of `not is_global`: ranges Python's table counts as global, or that must be
# named in the refusal. (label, networks)
_V4_KINDS: tuple[tuple[str, tuple[IPNetwork, ...]], ...] = (
    ("the unspecified address", _nets("0.0.0.0/8")),
    ("a loopback address", _nets("127.0.0.0/8")),
    ("a private network address", _nets("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")),
    ("a link-local address (cloud metadata lives here)", _nets("169.254.0.0/16")),
    ("a shared (carrier-grade NAT) address", _nets("100.64.0.0/10")),
    ("a special-purpose address", _nets("192.0.0.0/24", "198.18.0.0/15")),
    ("a multicast address", _nets("224.0.0.0/4")),
    ("a reserved address", _nets("240.0.0.0/4")),
)
_V6_KINDS: tuple[tuple[str, tuple[IPNetwork, ...]], ...] = (
    ("the unspecified address", _nets("::/128")),
    ("a loopback address", _nets("::1/128")),
    ("a private network address", _nets("fc00::/7", "fec0::/10")),
    ("a link-local address", _nets("fe80::/10")),
    ("a multicast address", _nets("ff00::/8")),
    ("a Teredo tunnel address", _nets("2001::/32")),
    ("a local-use NAT64 address", _nets("64:ff9b:1::/48")),
)
# Never fetched, whatever the allow list says: they are not a server anyone can mean.
_NEVER = _nets("0.0.0.0/8", "224.0.0.0/4", "255.255.255.255/32", "::/128", "ff00::/8")

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_V4_COMPAT = ipaddress.ip_network("::/96")
_V4_TRANSLATED = ipaddress.ip_network("::ffff:0:0:0/96")


def embedded_ipv4(ip: IPAddress) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 address carries (mapped, compatible, translated, 6to4, NAT64)."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return None
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    low = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in _NAT64 or ip in _V4_TRANSLATED:
        return low
    if ip in _V4_COMPAT and int(ip) > 1:  # not :: or ::1
        return low
    return None


def _plain(ip: IPAddress) -> IPAddress:
    """The address without an IPv6 zone (`fe80::1%eth0`): zones do not change the range."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.scope_id:
        return ipaddress.IPv6Address(str(ip).split("%", 1)[0])
    return ip


def address_problem(ip: IPAddress) -> str | None:
    """Why `ip` is not a public address (a phrase for the refusal), or None when it is."""
    ip = _plain(ip)
    inner = embedded_ipv4(ip)
    if inner is not None:
        problem = address_problem(inner)
        return f"{problem} inside an IPv6 address" if problem else None
    kinds = _V4_KINDS if isinstance(ip, ipaddress.IPv4Address) else _V6_KINDS
    for label, nets in kinds:
        if any(ip in net for net in nets):
            return label
    if not ip.is_global or ip.is_multicast:
        return "a reserved or non-public address"
    return None


def _forms(ip: IPAddress) -> tuple[IPAddress, ...]:
    """The address and, for an IPv6 address that carries one, its IPv4 address."""
    ip = _plain(ip)
    inner = embedded_ipv4(ip)
    return (ip,) if inner is None else (ip, inner)


def _in_any(ip: IPAddress, nets: tuple[IPNetwork, ...]) -> bool:
    return any(form.version == net.version and form in net for form in _forms(ip) for net in nets)


def normalize_host(host: str) -> str:
    return host.strip().lower().rstrip(".")


def parse_entries(value: str | list[str] | None, setting: str) -> tuple[tuple[IPNetwork, ...], frozenset[str]]:
    """A comma list of CIDR networks / addresses and exact host names; raises ValueError."""
    if value is None:
        return (), frozenset()
    items = value.split(",") if isinstance(value, str) else list(value)
    nets: list[IPNetwork] = []
    hosts: set[str] = set()
    for raw in items:
        entry = str(raw).strip()
        if not entry:
            continue
        try:
            nets.append(ipaddress.ip_network(entry.strip("[]"), strict=False))
            continue
        except ValueError:
            pass
        host = normalize_host(entry)
        labels = host.split(".")
        if not host or any(not label or len(label) > 63 for label in labels) or not all(
                ch.isalnum() or ch in "-." for ch in host) or "/" in entry:
            raise ValueError(f"{setting}: {entry!r} is neither a network (CIDR such as "
                             f"192.168.1.0/24) nor a host name (such as nas.lan)")
        hosts.add(host)
    return tuple(nets), frozenset(hosts)


@dataclass(frozen=True)
class AddressPolicy:
    """The allow and deny lists, and the check every connection goes through."""

    allow_nets: tuple[IPNetwork, ...] = ()
    allow_hosts: frozenset[str] = frozenset()
    deny_nets: tuple[IPNetwork, ...] = ()
    deny_hosts: frozenset[str] = frozenset()

    @classmethod
    def from_lists(cls, allow: str | list[str] | None, deny: str | list[str] | None) -> AddressPolicy:
        allow_nets, allow_hosts = parse_entries(allow, "MORPHYARD_URL_ALLOW")
        deny_nets, deny_hosts = parse_entries(deny, "MORPHYARD_URL_DENY")
        return cls(allow_nets, allow_hosts, deny_nets, deny_hosts)

    def describe(self) -> str:
        def show(nets, hosts):
            items = [str(n) for n in nets] + sorted(hosts)
            return ", ".join(items) if items else "none"
        return (f"allow {show(self.allow_nets, self.allow_hosts)}; "
                f"deny {show(self.deny_nets, self.deny_hosts)}")

    def check(self, host: str, addresses: list[IPAddress]) -> list[IPAddress]:
        """The addresses to connect to, in order; raises UrlRefusedError unless every one may
        be reached (one refused address refuses the host)."""
        name = normalize_host(host)
        if name in self.deny_hosts:
            raise UrlRefusedError(f"The host {name} is on this server's deny list "
                                  f"(MORPHYARD_URL_DENY).")
        if not addresses:
            raise UrlInputError(f"The host {name} has no address.")
        allowed_host = name in self.allow_hosts
        for ip in addresses:
            if _in_any(ip, _NEVER):
                raise UrlRefusedError(
                    f"The host {name} resolves to {_plain(ip)}, {address_problem(ip)}, which is "
                    f"never fetched.")
            if _in_any(ip, self.deny_nets):
                raise UrlRefusedError(f"The host {name} resolves to {_plain(ip)}, which is on this "
                                      f"server's deny list (MORPHYARD_URL_DENY).")
            problem = address_problem(ip)
            if problem and not (allowed_host or _in_any(ip, self.allow_nets)):
                raise UrlRefusedError(
                    f"The host {name} resolves to {_plain(ip)}, {problem}. This server only "
                    f"fetches public addresses, so a URL cannot reach its own machine or "
                    f"network. Ask the user for the file itself; an administrator can allow "
                    f"a LAN host with MORPHYARD_URL_ALLOW.")
        return list(addresses)


__all__ = [
    "AddressPolicy", "IPAddress", "UrlInputError", "UrlRefusedError", "address_problem",
    "embedded_ipv4", "normalize_host", "parse_entries",
]
