#  Project:      dfe-engine
#  File:         clickhouse/tls.py
#  Purpose:      Single TLS posture resolver for every clickhouse-connect client
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""One TLS posture, resolved once, for every clickhouse-connect client the
engine mints.

``ClickHouseManager``, ``ConnectionRegistry``, the restricted query client,
the hunt-runner CLI and the KEDA shim each build their own clickhouse-connect
client; every one of them resolves its secure/verify/ca_cert kwargs through
:func:`resolve_clickhouse_tls` so a CA path or a verify override applies
identically everywhere, and an unreadable CA file is refused up front rather
than silently connecting without a trust anchor.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scalo.crypto import tls_parts


class ClickHouseCaCertUnreadable(RuntimeError):
    """DFE_CLICKHOUSE_CA_CERT names a file the process cannot open."""


@dataclass(frozen=True, slots=True)
class ClickHouseTls:
    """Resolved TLS posture for one clickhouse-connect client.

    ``secure`` off carries ``verify=True``/``ca_cert=None`` inertly --
    clickhouse-connect never reads either on the plain-HTTP path, so a caller
    that always applies :meth:`connect_kwargs` gets no behaviour change when
    secure is off.

    Attributes:
        secure: Whether the connection uses HTTPS.
        verify: Whether the server certificate is verified (HTTPS only).
        ca_cert: PEM CA file path trusted for verification, if any.
    """

    secure: bool
    verify: bool
    ca_cert: str | None

    def connect_kwargs(self) -> dict[str, Any]:
        """The secure/verify/ca_cert kwargs for ``clickhouse_connect.get_client``.

        Returns:
            An empty dict when ``secure`` is off; otherwise ``secure``,
            ``verify``, and ``ca_cert`` (only when set).
        """
        if not self.secure:
            return {}
        kwargs: dict[str, Any] = {"secure": True, "verify": self.verify}
        if self.ca_cert:
            kwargs["ca_cert"] = self.ca_cert
        return kwargs


def resolve_clickhouse_tls(
    *, secure: bool, verify: bool | None, ca_cert: str | None
) -> ClickHouseTls:
    """Resolve one ClickHouse client's TLS posture.

    The single builder every clickhouse-connect call site in the engine goes
    through. Verify defaults ON whenever ``secure`` is on (scalo's
    ``tls_parts``, via ``DFE_CLICKHOUSE_VERIFY`` or the ``SCALO_TLS_VERIFY``
    escape valve); turning it off is an explicit setting and scalo logs one
    warning per process. A ``ca_cert`` path that does not exist or cannot be
    opened raises before any client is built, naming
    ``DFE_CLICKHOUSE_CA_CERT``, rather than clickhouse-connect silently
    connecting without a trust anchor.

    Args:
        secure: Whether the connection uses HTTPS.
        verify: Explicit verify override, or None to defer to the scalo
            env escape valve.
        ca_cert: PEM CA file path, or None.

    Returns:
        The resolved TLS posture.

    Raises:
        ClickHouseCaCertUnreadable: ``ca_cert`` is set but cannot be opened.
    """
    if not secure:
        return ClickHouseTls(secure=False, verify=True, ca_cert=None)
    if ca_cert is not None:
        _check_ca_cert_readable(ca_cert)
    tls = tls_parts(ca_paths=[ca_cert] if ca_cert else None, verify=verify)
    return ClickHouseTls(
        secure=True,
        verify=tls.verify,
        ca_cert=tls.ca_paths[0] if tls.ca_paths else None,
    )


def _check_ca_cert_readable(ca_cert: str) -> None:
    """Raise naming DFE_CLICKHOUSE_CA_CERT if ``ca_cert`` cannot be opened.

    Args:
        ca_cert: PEM CA file path to check.

    Raises:
        ClickHouseCaCertUnreadable: The file does not exist or cannot be read.
    """
    try:
        with Path(ca_cert).open("rb"):
            pass
    except OSError as exc:
        raise ClickHouseCaCertUnreadable(
            f"DFE_CLICKHOUSE_CA_CERT ({ca_cert}) could not be read: {exc}"
        ) from exc
