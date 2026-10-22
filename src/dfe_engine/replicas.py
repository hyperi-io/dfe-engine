#  Project:      dfe-engine
#  File:         replicas.py
#  Purpose:      This engine process among its replicas, and the guard on running more than one
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""This engine process among its replicas: its instance id, and the guard on running several.

One engine can keep its users, the secrets it mints and its deploy repo where only
it reaches them. A second replica works only once each of those is shared, so
:func:`require_shared_state` refuses the boot while any is not.
"""

import functools
import socket
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dfe_engine.settings import DFESettings

# The secrets providers whose create is atomic across processes: OpenBao KV v2 with
# cas=0, and AWS CreateSecret. Two replicas minting one secret then keep one value.
SHARED_SECRETS_PROVIDERS = frozenset({"openbao", "vault", "aws"})


class UnsharedStateError(ValueError):
    """More than one replica was asked for while some state lives where only one engine reaches it."""


@functools.cache
def instance_id() -> str:
    """This process's id among the engine's replicas: the hostname and a UUID drawn at boot.

    The hostname says which pod, and the UUID tells a restarted process from the one
    before it on the same pod.
    """
    return f"{socket.gethostname()}-{uuid.uuid4().hex}"


def unshared_state(settings: DFESettings) -> list[str]:
    """Each setting that keeps state on one engine, with the variable that sets it."""
    found = []
    if settings.auth.store_backend.lower() != "document":
        found.append(
            f"auth.store_backend is {settings.auth.store_backend!r}, not 'document' "
            "(DFE_AUTH_STORE_BACKEND)"
        )
    if settings.secrets.provider not in SHARED_SECRETS_PROVIDERS:
        found.append(
            f"secrets.provider is {settings.secrets.provider!r}, not openbao, vault or aws "
            "(DFE_SECRETS_PROVIDER)"
        )
    if not settings.gitops.enabled:
        found.append("gitops.enabled is off (DFE_GITOPS_ENABLED)")
    if not settings.gitops.repo_url:
        found.append(
            "gitops.repo_url is empty, so the deploy repo has no remote (DFE_GITOPS_REPO_URL)"
        )
    if not settings.gitops.push:
        found.append(
            "gitops.push is off, so no other replica sees this one's commits (DFE_GITOPS_PUSH)"
        )
    return found


def require_shared_state(settings: DFESettings) -> None:
    """Refuse to run more than one replica while any state stays on one engine.

    Raises:
        UnsharedStateError: ``engine_replicas`` is above 1 and :func:`unshared_state`
            names at least one setting. The message names every one.
    """
    if settings.engine_replicas <= 1:
        return
    found = unshared_state(settings)
    if found:
        raise UnsharedStateError(
            f"DFE_ENGINE_REPLICAS={settings.engine_replicas} needs state every replica "
            f"reaches: {'. '.join(found)}"
        )
