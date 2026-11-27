#  Project:      dfe-engine
#  File:         outbound_url.py
#  Purpose:      Refuse an operator-supplied URL the engine would call into its own pod or cloud
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The check every URL a caller makes the engine call goes through.

A sigma feed, an OIDC issuer and an alert webhook are all fetched from the engine's
pod or the hunt runner's, so a caller who picks the URL picks where the engine
connects. The check refuses loopback, link-local (the cloud metadata endpoint among
them), unspecified, multicast and reserved hosts. A private RFC1918 host is allowed,
because internal feeds and IdPs are legitimate.
"""

import ipaddress
import socket
from collections.abc import Collection
from urllib.parse import urlparse

from scalo.api import ProblemError

_BLOCKED_NAMES = frozenset({"localhost", "metadata", "metadata.google.internal"})


def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved
    )


def refuse_blocked_host(host: str, field: str, *, resolve_port: int | None = None) -> None:
    """Refuse *host* when it names, or with *resolve_port* resolves to, an address the engine may not call.

    Without *resolve_port* only the name as written is checked, for a URL whose host
    part may be a credential that must not reach a DNS resolver. A lookup that fails is
    not a refusal, since a network-restricted pod still has to save its config.

    Raises:
        ProblemError: 422 ``host_blocked``.
    """
    name = host.rstrip(".").lower()
    if name in _BLOCKED_NAMES:
        raise ProblemError(422, f"{field} host {name!r} is blocked", code="host_blocked")
    try:
        literal = ipaddress.ip_address(name)
    except ValueError:
        literal = None
    if literal is not None:
        if _blocked(literal):
            raise ProblemError(
                422, f"{field} host {name!r} is blocked ({literal})", code="host_blocked"
            )
        return
    if resolve_port is None:
        return
    try:
        infos = socket.getaddrinfo(name, resolve_port, proto=socket.IPPROTO_TCP)
    except OSError, UnicodeError:
        infos = []
    for info in infos:
        if _blocked(ipaddress.ip_address(info[4][0])):
            raise ProblemError(
                422, f"{field} host {name!r} resolves to a blocked address", code="host_blocked"
            )


def confine_outbound_url(url: str, field: str, *, allowed_hosts: Collection[str] = ()) -> None:
    """Refuse *url* unless it is http(s) to a host the engine may call.

    Args:
        url: The URL as the caller sent it.
        field: The request field, named in the refusal.
        allowed_hosts: Lower-case hostnames the URL must use, or empty for any.

    Raises:
        ProblemError: 422 ``bad_url_scheme``, ``bad_url``, ``host_blocked`` or ``host_not_allowed``.
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ProblemError(422, f"{field} must be http(s)", code="bad_url_scheme")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise ProblemError(422, f"{field} has no host", code="bad_url")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise ProblemError(422, f"{field} has an invalid port", code="bad_url") from None
    refuse_blocked_host(host, field, resolve_port=port)
    if allowed_hosts and host not in allowed_hosts:
        raise ProblemError(
            422, f"{field} host {host!r} is not an allowed host", code="host_not_allowed"
        )
