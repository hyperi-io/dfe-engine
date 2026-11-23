#  Project:      dfe-engine
#  File:         appmgmt/argo_refresh.py
#  Purpose:      Ask Argo CD to hard-refresh the Applications a deploy-repo push changed
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Hard-refresh the Argo CD Applications a push changed, and read their state.

A refresh Argo starts from a webhook can be folded into a compare that is already
running, and that compare read the repository before the push. The Application then
reports Synced against the old revision until the next push. Annotating the
Application ``argocd.argoproj.io/refresh=hard`` after the push starts a compare that
re-reads the revision.

The engine talks to the Kubernetes API with its pod's service account, which the
chart's Role limits to get, list and patch on Applications in Argo's namespace.
Everything here is best effort: the push has already landed, so a refused or
unreachable API is logged and the sync result is unchanged. ``read_application``
uses the same client for the GET behind an instance's status.
"""

import os
import ssl
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx
from scalo.crypto import CryptoProfile, ssl_context
from scalo.logger import logger

ARGO_NAMESPACE = "argocd"
REFRESH_ANNOTATION = "argocd.argoproj.io/refresh"
MERGE_PATCH = "application/merge-patch+json"

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_TIMEOUT_SECONDS = 5.0


def application_url(host: str, port: str, name: str) -> str:
    """The Kubernetes API path of the Argo Application *name* in Argo's namespace."""
    return (
        f"https://{host}:{port}/apis/argoproj.io/v1alpha1"
        f"/namespaces/{ARGO_NAMESPACE}/applications/{name}"
    )


def _api_port() -> str:
    """The port the Kubernetes API listens on, as the pod was started against it."""
    return os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS") or os.environ.get(
        "KUBERNETES_SERVICE_PORT", "443"
    )


def _api_client(
    host: str,
    *,
    transport: httpx.BaseTransport | None,
    service_account: Path,
    crypto_profile: CryptoProfile,
    content_type: str | None = None,
) -> httpx.Client | None:
    """Return a client that carries the pod's token, or None when there is no cluster to call.

    Off a cluster there is no host to dial, and with no service account mounted there
    is no token to send.
    """
    if not host and transport is None:
        return None
    try:
        token = (service_account / "token").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    ca = service_account / "ca.crt"
    headers = {"Authorization": f"Bearer {token}"}
    if content_type is not None:
        headers["Content-Type"] = content_type
    try:
        verify = ssl_context(crypto_profile, cafile=str(ca) if ca.is_file() else None)
    except (OSError, ssl.SSLError) as exc:
        _warn_once("ca", "argo client unavailable: the service account CA is unreadable", str(exc))
        return None
    return httpx.Client(
        transport=transport,
        verify=verify,
        timeout=_TIMEOUT_SECONDS,
        headers=headers,
    )


def refresh_applications(
    names: Iterable[str],
    *,
    transport: httpx.BaseTransport | None = None,
    service_account: Path = SERVICE_ACCOUNT,
    crypto_profile: CryptoProfile = CryptoProfile.PROD,
) -> list[str]:
    """Annotate each named Application for a hard refresh and return the ones that failed.

    Never raises. Off a cluster, with no service account mounted, nothing is sent and
    every name is returned as not refreshed.

    Args:
        names: Argo Application names, deduplicated here.
        transport: An httpx transport in place of the cluster connection, for tests.
        service_account: The mounted service account directory.
        crypto_profile: TLS profile of the connection to the Kubernetes API.
    """
    wanted = list(dict.fromkeys(names))
    if not wanted:
        return []
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
    port = _api_port()
    client = _api_client(
        host,
        transport=transport,
        service_account=service_account,
        crypto_profile=crypto_profile,
        content_type=MERGE_PATCH,
    )
    if client is None:
        logger.warning("argo refresh skipped: no cluster API or service account token")
        return wanted
    failed: list[str] = []
    with client:
        for name in wanted:
            body = {"metadata": {"annotations": {REFRESH_ANNOTATION: "hard"}}}
            try:
                response = client.patch(application_url(host, port, name), json=body)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("argo hard refresh failed", application=name, error=str(exc))
                failed.append(name)
    return failed


def read_application(
    name: str,
    *,
    transport: httpx.BaseTransport | None = None,
    service_account: Path = SERVICE_ACCOUNT,
    crypto_profile: CryptoProfile = CryptoProfile.PROD,
) -> dict[str, Any] | None:
    """Return the Argo Application *name* as the Kubernetes API serves it, or None.

    Off a cluster, or with no service account mounted, nothing is sent and None comes
    back without a log line, because a status call asks on every poll. A refused,
    missing or slow API logs one warning and also returns None.

    Args:
        name: Argo Application name.
        transport: An httpx transport in place of the cluster connection, for tests.
        service_account: The mounted service account directory.
        crypto_profile: TLS profile of the connection to the Kubernetes API.

    Returns:
        The Application object, its ``status`` as Argo CD wrote it.
    """
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
    client = _api_client(
        host,
        transport=transport,
        service_account=service_account,
        crypto_profile=crypto_profile,
    )
    if client is None:
        return None
    with client:
        try:
            response = client.get(application_url(host, _api_port(), name))
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            _warn_once(name, "argo application read failed", type(exc).__name__, str(exc))
            return None
    _warned.pop(name, None)
    return body if isinstance(body, dict) else None


_warned: dict[str, str] = {}
"""The last failure logged per key: a status poll repeats a failed read every few seconds."""


def _warn_once(key: str, message: str, kind: str, detail: str = "") -> None:
    """Log ``message`` for ``key`` only when its failure ``kind`` differs from the last logged."""
    if _warned.get(key) == kind:
        return
    _warned[key] = kind
    logger.warning(message, target=key, error=detail or kind)
