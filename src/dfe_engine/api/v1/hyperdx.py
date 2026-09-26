#  Project:      dfe-engine
#  File:         api/v1/hyperdx.py
#  Purpose:      RBAC'd HyperDX reads: per-org connection material, and source placement
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The fork's two reads: the caller's org connection, and where its sources landed.

GET /api/v1/hyperdx/connection - the caller's OWN org ClickHouse connection.

The fork calls this with the user's forwarded dfe_token to seed exactly ONE
connection on that user's team: the pinned ``dfe_org_<org>`` CH user, or the
platform reader for unrestricted roles. The connection's CH user IS the tenant
isolation (its row policy forces ``WHERE _org_id`` server-side), so a caller can
only ever receive credentials for their own org and a cross-org ask is a 403.
This retires the global ``DEFAULT_CONNECTIONS`` blob that handed every team every
org's connection (dfe-engine#124).

A caller reads UNRESTRICTED (the platform reader) only when a role other than
``org_viewer`` grants it ``query:execute`` at SYSTEM scope. Every other caller is
fenced to its single org, whatever else it holds: a role bound at one org's scope
never reaches every org's rows. A caller that resolves to no single org fails
closed. The ``query:execute`` gate keeps callers with no data-plane access out.

GET /api/v1/hyperdx/sources - every HyperDX team and the DFE sources on it.

A deploy writes its source to every team over that team's own connection, so this
is the read that says where it landed; ``source:read`` gates it.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, Settings, is_action_allowed, require_action
from dfe_engine.auth import Scope, ScopedGrant
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.governance.ch.bindings import ORG_VIEWER_ROLE
from dfe_engine.governance.ch.models import org_user_name

router = APIRouter(prefix="/hyperdx", tags=["HyperDX"])

# The minted platform reader (a service role, no tenant role -> reads across orgs,
# tier-limited). Handed only to a caller that passes _reads_every_org.
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


def _require_query_execute(request: Request, user) -> None:
    """Gate the connection on ``query:execute``, evaluated scope-aware.

    An org_viewer holds ``query:execute`` only at its OWN org scope, so a plain
    system-scope check (the default ``require_action``) locks every org_viewer out
    of its own connection - the exact caller this endpoint exists to serve. Pass
    if the caller can execute at system scope OR at any org they belong to.
    """
    action = scopes_dict["query_execute"]
    if is_action_allowed(request, user, action):
        return
    for org_id in user.org_ids:
        if is_action_allowed(request, user, action, scope=Scope(type="org", id=org_id)):
            return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "forbidden",
            "message": "query:execute required (at system or your org scope)",
        },
    )


def _reads_every_org(request: Request, user) -> bool:
    """Whether a role other than ``org_viewer`` grants ``query:execute`` at SYSTEM scope.

    ``org_viewer`` is the tenant role, so a grant of it never unfences the caller,
    and a role bound at an org's scope covers that org alone.
    """
    # Bare roles are system-scope grants, as authorize() reads a context without grants.
    grants = user.grants or [ScopedGrant(role=name) for name in user.roles]
    platform = [grant for grant in grants if grant.role != ORG_VIEWER_ROLE]
    if not platform:
        return False
    beyond_viewer = user.model_copy(
        update={"roles": [grant.role for grant in platform], "grants": platform}
    )
    return is_action_allowed(request, beyond_viewer, scopes_dict["query_execute"])


@router.get(
    "/connection",
    response_model=HyperDXConnection,
)
async def hyperdx_connection(
    request: Request,
    user: CurrentUser,
    settings: Settings,
) -> HyperDXConnection:
    """Return the caller's OWN org connection - never another org's.

    A caller granted ``query:execute`` at system scope by a role other than
    ``org_viewer`` gets the platform reader. Any other caller gets its org's pinned
    ``dfe_org_<org>`` user, whatever roles it holds at that org's scope. A caller
    that resolves to zero or several separate orgs is refused (403) so isolation
    fails closed rather than guessing.
    """
    _require_query_execute(request, user)

    from dfe_engine.secrets import build_secrets

    store = build_secrets(settings.secrets)
    host = _ch_host_url(settings)

    def _secret(path: str) -> str:
        return store.get(path) if store.exists(path) else ""

    if _reads_every_org(request, user):
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


class HyperDXTeamSource(BaseModel):
    """One HyperDX source on one team, as the fork holds it."""

    id: str = Field(description="HyperDX source id on that team")
    name: str = Field(description="DFE source name; the HyperDX source carries the same one")
    table: dict[str, str] = Field(
        default_factory=dict,
        description="The ClickHouse table the source reads (databaseName, tableName)",
    )


class HyperDXTeamSources(BaseModel):
    """One HyperDX team and the DFE sources it holds."""

    team: str = Field(description="HyperDX team id")
    team_name: str = Field(description="HyperDX team name; the caller's OIDC group")
    sources: list[HyperDXTeamSource] = Field(default_factory=list)


class HyperDXSourcesResponse(BaseModel):
    """Where every deployed DFE source actually landed in HyperDX."""

    teams: list[HyperDXTeamSources] = Field(default_factory=list)


@router.get(
    "/sources",
    response_model=HyperDXSourcesResponse,
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
async def hyperdx_sources(request: Request) -> HyperDXSourcesResponse:
    """List every HyperDX team and the DFE sources on it.

    A deploy writes its source to every team, so this is the read that says where
    it landed. 503 when HyperDX is not deployed or not answering -- an empty list
    would read as "the source is missing", which is a different fault.
    """
    client = getattr(request.app.state, "hyperdx_client", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "hyperdx_absent", "message": "this deployment has no HyperDX"},
        )

    from dfe_engine.hyperdx.sources import list_sources_by_team

    listing = await list_sources_by_team(client)
    if listing is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "hyperdx_unreachable", "message": "HyperDX did not answer"},
        )

    return HyperDXSourcesResponse(
        teams=[
            HyperDXTeamSources(
                team=str(entry.get("team", "")),
                team_name=str(entry.get("teamName", "")),
                sources=[
                    HyperDXTeamSource(
                        id=str(source.get("id", "")),
                        name=str(source.get("name", "")),
                        table={
                            key: str(value)
                            for key, value in (source.get("from") or {}).items()
                            if isinstance(value, str)
                        },
                    )
                    for source in entry.get("sources") or []
                ],
            )
            for entry in listing
        ]
    )
