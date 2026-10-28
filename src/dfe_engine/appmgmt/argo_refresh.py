#  Project:      dfe-engine
#  File:         appmgmt/argo_refresh.py
#  Purpose:      Ask Argo CD to hard-refresh the Applications a deploy-repo push changed
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Hard-refresh the Argo CD Applications a push changed.

A refresh Argo starts from a webhook can be folded into a compare that is already
running, and that compare read the repository before the push. The Application then
reports Synced against the old revision until the next push. Annotating the
Application ``argocd.argoproj.io/refresh=hard`` after the push starts a compare that
re-reads the revision.

The engine talks to the Kubernetes API with its pod's service account, which the
chart's Role limits to get, list and patch on Applications in Argo's namespace.
Everything here is best effort: the push has already landed, so a refused or
unreachable API is logged and the sync result is unchanged.
"""

import os
from collections.abc import Iterable
from pathlib import Path

import httpx
from scalo.logger import logger

ARGO_NAMESPACE = "argocd"
REFRESH_ANNOTATION = "argocd.argoproj.io/refresh"

_SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_TIMEOUT_SECONDS = 5.0


def application_url(host: str, port: str, name: str) -> str:
    """The Kubernetes API path of the Argo Application *name* in Argo's namespace."""
    return (
        f"https://{host}:{port}/apis/argoproj.io/v1alpha1"
        f"/namespaces/{ARGO_NAMESPACE}/applications/{name}"
    )


def refresh_applications(
    names: Iterable[str],
    *,
    transport: httpx.BaseTransport | None = None,
    service_account: Path = _SERVICE_ACCOUNT,
) -> list[str]:
    """Annotate each named Application for a hard refresh and return the ones that failed.

    Never raises. Off a cluster, with no service account mounted, nothing is sent and
    every name is returned as not refreshed.

    Args:
        names: Argo Application names, deduplicated here.
        transport: An httpx transport in place of the cluster connection, for tests.
        service_account: The mounted service account directory.
    """
    wanted = list(dict.fromkeys(names))
    if not wanted:
        return []
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
    port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS") or os.environ.get(
        "KUBERNETES_SERVICE_PORT", "443"
    )
    try:
        token = (service_account / "token").read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("argo refresh skipped: no service account token", error=str(exc))
        return wanted
    if not host and transport is None:
        logger.warning("argo refresh skipped: not running in a cluster")
        return wanted
    failed: list[str] = []
    ca = service_account / "ca.crt"
    with httpx.Client(
        transport=transport,
        verify=str(ca) if ca.is_file() else True,
        timeout=_TIMEOUT_SECONDS,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/merge-patch+json",
        },
    ) as client:
        for name in wanted:
            body = {"metadata": {"annotations": {REFRESH_ANNOTATION: "hard"}}}
            try:
                response = client.patch(application_url(host, port, name), json=body)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("argo hard refresh failed", application=name, error=str(exc))
                failed.append(name)
    return failed
