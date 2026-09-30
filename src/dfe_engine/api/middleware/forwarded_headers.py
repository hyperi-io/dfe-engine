#  Project:      dfe-engine
#  File:         src/dfe_engine/api/middleware/forwarded_headers.py
#  Purpose:      The client address and scheme a trusted proxy forwarded, decided once per request
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The client address and scheme behind the proxies ``api.forwarded_allow_ips`` trusts.

A proxy that appends to X-Forwarded-For puts the address it saw on the right, so
whatever the client sent itself sits on the left. Reading right to left past every
trusted proxy reaches the first address no trusted hop vouches for, and that is
the client. A request whose TCP peer is not trusted keeps the peer, whatever
headers it carries.

The middleware rewrites ``scope["client"]`` and ``scope["scheme"]`` before any
route runs, so ``request.client`` and ``request.url`` carry the forwarded values
everywhere below it.
"""

import ipaddress
from dataclasses import dataclass

from starlette.types import ASGIApp, Receive, Scope, Send

type _Address = ipaddress.IPv4Address | ipaddress.IPv6Address
type _Network = ipaddress.IPv4Network | ipaddress.IPv6Network

_FORWARDED_SCHEMES = frozenset({"http", "https", "ws", "wss"})
_MAX_PORT = 65535


def _parse_address(host: str) -> _Address | None:
    """The IP address *host* names, or None when it names none."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    # A dual-stack listener reports an IPv4 peer as ::ffff:a.b.c.d.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _parse_port(text: str) -> int | None:
    """The port *text* names, or None when it is not a decimal port number."""
    if not (text.isascii() and text.isdecimal()):
        return None
    port = int(text)
    return port if port <= _MAX_PORT else None


def _parse_hop(entry: str) -> tuple[str, int] | None:
    """Split one X-Forwarded-For entry into (host, port), or None when it names no IP.

    Accepts a bare address, IPv4 ``host:port`` and bracketed IPv6 ``[host]:port``.
    A missing port reads as 0.
    """
    entry = entry.strip()
    host, port = entry, 0
    if entry.startswith("["):
        host, closed, rest = entry[1:].partition("]")
        if not closed:
            return None
        if rest:
            parsed = _parse_port(rest[1:]) if rest.startswith(":") else None
            if parsed is None:
                return None
            port = parsed
    elif entry.count(":") == 1:
        host, _, port_text = entry.partition(":")
        parsed = _parse_port(port_text)
        if parsed is None:
            return None
        port = parsed
    if _parse_address(host) is None:
        return None
    return host, port


@dataclass(frozen=True, slots=True)
class TrustedProxies:
    """The peers whose X-Forwarded-For and X-Forwarded-Proto are believed.

    Attributes:
        trust_all: Every peer and every hop is trusted, the ``*`` setting.
        networks: The trusted networks. A bare address is its own /32 or /128.
    """

    trust_all: bool = False
    networks: tuple[_Network, ...] = ()

    @classmethod
    def parse(cls, value: str) -> TrustedProxies:
        """Read ``api.forwarded_allow_ips``.

        Args:
            value: Comma-separated addresses and CIDR networks, ``*`` alone, or empty.

        Returns:
            The trust list. Empty trusts no peer.

        Raises:
            ValueError: An entry is neither an address nor a network, which would
                otherwise trust nothing without saying so.
        """
        if value.strip() == "*":
            return cls(trust_all=True)
        networks: list[_Network] = []
        for entry in (part.strip() for part in value.split(",")):
            if not entry:
                continue
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError as exc:
                raise ValueError(
                    f"api.forwarded_allow_ips: {entry!r} is not an IP address or CIDR network"
                ) from exc
        return cls(networks=tuple(networks))

    def trusts(self, host: str) -> bool:
        """Whether *host* is a trusted proxy. A host that is not an IP never is."""
        if self.trust_all:
            return True
        address = _parse_address(host)
        return address is not None and any(address in network for network in self.networks)

    def client(self, forwarded_for: str) -> tuple[str, int] | None:
        """The client a trusted peer's X-Forwarded-For names.

        The right-most entry that is not a trusted proxy, reading right to left. An
        entry that is not an IP address is skipped. When every entry is trusted the
        left-most is the client, because a trusted proxy made the request itself.

        Args:
            forwarded_for: Every X-Forwarded-For value on the request, comma-joined.

        Returns:
            (host, port), or None when no entry is an IP address.
        """
        leftmost: tuple[str, int] | None = None
        # Right to left, so a long chain the caller padded is parsed only as far as the client.
        for entry in reversed(forwarded_for.split(",")):
            hop = _parse_hop(entry)
            if hop is None:
                continue
            if not self.trusts(hop[0]):
                return hop
            leftmost = hop
        return leftmost


class ForwardedHeadersMiddleware:
    """Rewrite the client and scheme from a trusted peer's X-Forwarded-For and -Proto."""

    def __init__(self, app: ASGIApp, trusted: TrustedProxies) -> None:
        self.app = app
        self.trusted = trusted

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Apply a trusted peer's forwarded headers to *scope*, then call the app."""
        if scope["type"] in ("http", "websocket"):
            peer = scope.get("client")
            if peer is not None and self.trusted.trusts(peer[0]):
                self._apply(scope)
        await self.app(scope, receive, send)

    def _apply(self, scope: Scope) -> None:
        proto: str | None = None
        forwarded_for: list[str] = []
        for name, value in scope["headers"]:
            if name == b"x-forwarded-proto":
                proto = value.decode("latin-1").strip()
            elif name == b"x-forwarded-for":
                forwarded_for.append(value.decode("latin-1"))
        if proto is not None and proto in _FORWARDED_SCHEMES:
            websocket = scope["type"] == "websocket"
            scope["scheme"] = proto.replace("http", "ws") if websocket else proto
        if forwarded_for and (client := self.trusted.client(",".join(forwarded_for))):
            scope["client"] = client
