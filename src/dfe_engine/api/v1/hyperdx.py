#  Project:      dfe-engine
#  File:         api/v1/hyperdx.py
#  Purpose:      RBAC'd per-org ClickHouse connection material for the HyperDX fork
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""GET /api/v1/hyperdx/connection - the caller's OWN org ClickHouse connection.

The fork calls this with the user's forwarded dfe_token to seed exactly ONE
connection on that user's team: the pinned ``dfe_org_<org>`` CH user, or the
platform reader for unrestricted roles. The connection's CH user IS the tenant
isolation (its row policy forces ``WHERE _org_id`` server-side), so a caller can
only ever receive credentials for their own org and a cross-org ask is a 403.
This retires the global ``DEFAULT_CONNECTIONS`` blob that handed every team every
org's connection (dfe-engine#124).

Which identity a caller resolves to mirrors ``governance.ch.bindings`` exactly:
any role beyond ``org_viewer`` reads UNRESTRICTED (the platform reader); a caller
holding only ``org_viewer`` is fenced to its single org; anything else fails
closed. The ``query:execute`` gate keeps callers with no data-plane access out.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from dfe_engine.api.deps import CurrentUser, Settings, require_action
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.governance.ch.bindings import ORG_VIEWER_ROLE
from dfe_engine.governance.ch.models import org_user_name

router = APIRouter(prefix="/hyperdx", tags=["HyperDX"])

# The minted platform reader (a service role, no tenant role -> reads across orgs,
# tier-limited). Handed to any caller the binding model treats as unrestricted.
_PLATFORM_USERNAME = "dfe_query_reader"
_PLATFORM_SECRET = "ch/service/query_reader"


class HyperDXConnection(BaseModel):
    """Connection material in the fork's shape (name, host URL, username, password)."""

    name: str
    host: str
    username: str
    password: str


def _ch_host_url(settings) -> str:
    """The ClickHouse HTTP URL the fork's proxy connects to (engine's own CH host)."""
    ch = settings.clickhouse
    scheme = "https" if getattr(ch, "secure", False) else "http"
    return f"{scheme}://{ch.host}:{ch.port}"


@router.get(
    "/connection",
    response_model=HyperDXConnection,
    dependencies=[Depends(require_action(scopes_dict["query_execute"]))],
)
async def hyperdx_connection(
    request: Request,
    user: CurrentUser,
    settings: Settings,
) -> HyperDXConnection:
    """Return the caller's OWN org connection - never another org's.

    Unrestricted callers (any role beyond ``org_viewer``) get the platform reader;
    a single-org caller gets its pinned ``dfe_org_<org>`` user; a caller that
    resolves to zero or several separate orgs is refused (403) so isolation fails
    closed rather than guessing.
    """
    from dfe_engine.secrets import build_secrets

    store = build_secrets(settings.secrets)
    host = _ch_host_url(settings)

    def _secret(path: str) -> str:
        return store.get(path) if store.exists(path) else ""

    # Platform readers: mirrors derive_group_bindings - any role other than the
    # org-viewer role reads unrestricted. The org filter fences tenants in, never
    # the platform's own analysts out.
    if set(user.roles) - {ORG_VIEWER_ROLE}:
        password = _secret(_PLATFORM_SECRET)
        if not password:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "platform_reader_unprovisioned",
                    "message": "the platform ClickHouse reader has no credential yet",
                },
            )
        return HyperDXConnection(
            name="platform", host=host, username=_PLATFORM_USERNAME, password=password
        )

    # Org-scoped: resolve the caller to exactly ONE registered org. org_ids carry
    # both the owning org name and the group's tenant ids, so match on either.
    org_registry = getattr(request.app.state, "org_registry", None)
    orgs = list(org_registry.list()) if org_registry is not None else []
    caller = set(user.org_ids)
    matched = [o for o in orgs if o.name in caller or (set(o.org_ids) & caller)]

    if len(matched) != 1:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "no_single_org",
                "message": "caller does not resolve to exactly one org",
            },
        )

    org = matched[0]
    password = _secret(f"ch/orgs/{org.name}")
    if not password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "org_unprovisioned",
                "message": f"org '{org.name}' has no ClickHouse credential yet",
            },
        )
    return HyperDXConnection(
        name=org.name, host=host, username=org_user_name(org.name), password=password
    )
