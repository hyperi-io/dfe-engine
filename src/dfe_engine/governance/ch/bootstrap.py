#  Project:      dfe-engine
#  File:         governance/ch/bootstrap.py
#  Purpose:      Wire settings + the RBAC stores into a CH-RBAC reconcile
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The shared assembly between settings/stores and ``reconcile_ch_rbac``.

Every caller - app startup, the governance endpoint, and the trigger an org or
group change fires - needs the same admin client, secrets store, orgs and
bindings, and previously built all four inline; the copies drifted, which is how
``bindings`` came to be passed by neither.

Building the client stays a separate call so each caller keeps its own failure
policy: startup logs and continues, the endpoint returns 503, the trigger counts
the failure and retries after a back-off.

Imports are function-local because the rest of the ``governance.ch`` package is
pure rendering with no cluster or settings dependency.
"""

import os
import threading
from typing import Any

from scalo.logger import logger

from .bindings import derive_group_bindings
from .models import DEFAULT_SERVICE_ROLES
from .reconciler import ReconcileResult, reconcile_ch_rbac, reconcile_ch_service_roles

_ENABLED_VALUES = ("true", "1", "yes")
_DISABLED_VALUES = ("false", "0", "no")

# Two runs minting the same new identity at once store one password and give
# ClickHouse the other, so the endpoint, startup and the change trigger take turns.
_RECONCILE_LOCK = threading.Lock()

TENANT_ISOLATION_ENV = "DFE_TENANT_ISOLATION_ENABLED"
LEGACY_TENANT_ISOLATION_ENV = "DFE_ORG_PROVISIONING_ENABLED"


def tenant_isolation_enabled() -> bool:
    """Whether to reconcile the CH tenant fence, defaulting to ON.

    The row policies this gates ARE the isolation, so a deployment that leaves it
    unset gets enforcement rather than `_org_id` columns nothing reads.
    """
    for env in (TENANT_ISOLATION_ENV, LEGACY_TENANT_ISOLATION_ENV):
        raw = os.environ.get(env, "").strip().lower()
        if not raw:
            continue
        if env == LEGACY_TENANT_ISOLATION_ENV:
            logger.warning(f"{env} is deprecated; use {TENANT_ISOLATION_ENV}")
        return raw in _ENABLED_VALUES
    return True


def ch_admin_client(settings: Any) -> Any:
    """An admin ClickHouse client built from settings. Raises if unreachable."""
    from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager

    ch_cfg = {
        "ch_host": settings.clickhouse.host,
        "ch_port": settings.clickhouse.port,
        "ch_username": settings.clickhouse.username,
        "ch_password": settings.clickhouse.password,
        "ch_secure": settings.clickhouse.secure,
        "ch_verify": settings.clickhouse.verify,
        "ch_ca_cert": settings.clickhouse.ca_cert,
    }
    return ClickHouseManager.get_instance(ch_cfg).get_clickhouse_client()._client


def reconcile_from_stores(
    admin_client: Any,
    *,
    settings: Any,
    org_registry: Any = None,
    group_store: Any = None,
    role_config: Any = None,
) -> ReconcileResult:
    """Reconcile CH RBAC from the org registry and the RBAC group store.

    A missing store contributes nothing rather than failing: an org registry with
    no groups still reconciles tiers, roles and row policies. ``role_config`` is
    the role definitions the group bindings read a role's ``scoped`` flag from;
    None reads the shipped ones. One run at a time in this process; a caller
    arriving mid-run waits for it.
    """
    from dfe_engine.secrets import build_secrets

    with _RECONCILE_LOCK:
        orgs = org_registry.list() if org_registry is not None else []
        groups = group_store.list() if group_store is not None else []
        return reconcile_ch_rbac(
            admin_client,
            secrets_store=build_secrets(settings.secrets),
            orgs=orgs,
            bindings=derive_group_bindings(groups, orgs, role_config=role_config),
            provided_passwords=provided_service_passwords(),
        )


def reconcile_service_roles_from_settings(admin_client: Any, *, settings: Any) -> ReconcileResult:
    """Reconcile the seeded service roles and their users, and nothing else.

    Startup runs this in place of :func:`reconcile_from_stores` when tenant
    isolation is off. Shares the lock, because both mint the same identities.
    """
    from dfe_engine.secrets import build_secrets

    with _RECONCILE_LOCK:
        return reconcile_ch_service_roles(
            admin_client,
            secrets_store=build_secrets(settings.secrets),
            provided_passwords=provided_service_passwords(),
        )


def provided_service_passwords() -> dict[str, str]:
    """Every seeded service role's deployment-provided password, keyed by role name."""
    from dfe_engine.settings import provided_service_password

    provided = {r.name: provided_service_password(r.name) for r in DEFAULT_SERVICE_ROLES}
    return {name: password for name, password in provided.items() if password}


def service_user_password(settings: Any, username: str) -> str:
    """The password a minted service user connects with, or "" when it has none yet.

    The deployment-provided password wins, as it does in the reconcile; otherwise
    it is the secret the reconcile stored. A user that is not a minted service
    user has no password here.
    """
    from dfe_engine.secrets import build_secrets
    from dfe_engine.settings import provided_service_password

    role = next((r for r in DEFAULT_SERVICE_ROLES if r.mint_user and r.user() == username), None)
    if role is None:
        return ""
    if provided := provided_service_password(role.name):
        return provided
    store = build_secrets(settings.secrets)
    path = role.secret_path()
    return store.get(path) if store.exists(path) else ""
