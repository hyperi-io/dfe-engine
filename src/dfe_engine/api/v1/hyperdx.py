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

A caller reads UNRESTRICTED (the platform reader) only when a declared, unscoped
role other than ``org_viewer`` grants it ``query:execute`` at SYSTEM scope. Every
other caller is fenced to its single org, whatever else it holds: a role bound at
one org's scope, or marked ``scoped``, never reaches every org's rows. A caller
that resolves to no single org fails closed. The ``query:execute`` gate keeps
callers with no data-plane access out.

The fork names a caller's team after the username this read hands it, so
``GET /api/v1/auth/me`` reports the same username as ``hyperdx_identity``, decided
by the same function (:func:`hyperdx_identity`) and without the password.

GET /api/v1/hyperdx/sources - every HyperDX team and the DFE sources on it.

A deploy writes its source to every team over that team's own connection, so this
is the read that says where it landed. ``source:read`` gates it.
"""

from dataclasses import dataclass
from typing import NamedTuple

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from scalo.api import ProblemError
from scalo.concurrency import run_blocking

from dfe_engine.api.deps import (
    CurrentUser,
    Settings,
    allowed_at_system_or_own_org,
    held_orgs,
    reads_every_org,
    require_action,
    secrets_store,
)
from dfe_engine.api.v1.sources import held_source_orgs
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.governance.ch.models import DEFAULT_SERVICE_ROLES, org_user_name
from dfe_engine.schema.plan import object_names

router = APIRouter(prefix="/hyperdx", tags=["Search"])

# The minted platform reader (a service role, no tenant role -> reads across orgs,
# tier-limited). Handed only to a caller that passes _reads_every_org.
_PLATFORM_ROLE = next(role for role in DEFAULT_SERVICE_ROLES if role.name == "query_reader")

# dfe-schemas manifest ids of the tables the fork seeds a HyperDX source over.
_SEED_TABLE_IDS = (
    "data.main",
    "data.detection",
    "data.otel_logs",
    "data.otel_traces",
    "data.otel_metrics_gauge",
    "data.otel_metrics_sum",
    "data.otel_metrics_histogram",
    "data.otel_metrics_exponential_histogram",
    "data.otel_metrics_summary",
)


class HyperDXConnection(BaseModel):
    """Connection material in the fork's shape, plus where its seed sources live."""

    name: str
    host: str
    username: str
    password: str
    platform: bool = Field(description="True for the platform reader, which reads every org")
    database: str = Field(description="The DFE data database every seeded table lives in")
    tables: dict[str, str] = Field(description="Seeded table name by dfe-schemas manifest id")


def _ch_host_url(settings) -> str:
    """The ClickHouse HTTP URL the fork's proxy connects to (engine's own CH host)."""
    ch = settings.clickhouse
    scheme = "https" if getattr(ch, "secure", False) else "http"
    return f"{scheme}://{ch.host}:{ch.port}"


def _can_execute_queries(request: Request, user) -> bool:
    """Whether the caller holds ``query:execute`` at system scope or at one of its orgs.

    An org_viewer holds ``query:execute`` only at its OWN org scope, so a plain
    system-scope check (the default ``require_action``) locks every org_viewer out
    of its own connection - the exact caller this endpoint exists to serve.
    """
    return allowed_at_system_or_own_org(request, user, scopes_dict["query_execute"])


def _reads_every_org(request: Request, user) -> bool:
    """Whether a platform role grants ``query:execute`` at SYSTEM scope.

    ``platform_grants`` decides which grants may read across orgs, the same filter
    the CH group bindings use. This adds the ``query:execute`` check on top.
    """
    return reads_every_org(request, user, scopes_dict["query_execute"])


@dataclass(frozen=True, slots=True)
class ConnectionIdentity:
    """The ClickHouse user a caller reads as, and the secret path of its password."""

    name: str
    username: str
    secret_path: str
    platform: bool = False


class _Refusal(NamedTuple):
    code: str
    message: str


def _resolve_identity(request: Request, user) -> ConnectionIdentity | _Refusal:
    """Decide which ClickHouse user the caller reads as, or why it gets none."""
    if not _can_execute_queries(request, user):
        return _Refusal("forbidden", "query:execute required (at system or your org scope)")

    if _reads_every_org(request, user):
        return ConnectionIdentity(
            "platform", _PLATFORM_ROLE.user(), _PLATFORM_ROLE.secret_path(), platform=True
        )

    # A held caller reads as exactly one registered org, held as every org-held read holds it.
    matched = held_orgs(request, user, scopes_dict["query_execute"])
    if len(matched) != 1:
        return _Refusal("no_single_org", "caller does not resolve to exactly one org")

    org = matched[0]
    return ConnectionIdentity(org.name, org_user_name(org.name), f"ch/orgs/{org.name}")


def hyperdx_identity(request: Request, user) -> str:
    """The ClickHouse username ``GET /hyperdx/connection`` hands this caller.

    Args:
        request: The request, for the role config and the org registry.
        user: The caller's auth context.

    Returns:
        The username, or ``""`` when the connection read would refuse the caller.
    """
    identity = _resolve_identity(request, user)
    return identity.username if isinstance(identity, ConnectionIdentity) else ""


@router.get(
    "/connection",
    response_model=HyperDXConnection,
    summary="Get Search Connection",
)
async def hyperdx_connection(
    request: Request,
    user: CurrentUser,
    settings: Settings,
) -> HyperDXConnection:
    """Return the caller's OWN org connection - never another org's.

    A caller granted ``query:execute`` at system scope by a declared, unscoped role
    other than ``org_viewer`` gets the platform reader. Any other caller gets its org's pinned
    ``dfe_org_<org>`` user, whatever roles it holds at that org's scope. A caller
    that resolves to zero or several separate orgs is refused (403) so isolation
    fails closed rather than guessing.
    """
    identity = _resolve_identity(request, user)
    if isinstance(identity, _Refusal):
        raise ProblemError(status.HTTP_403_FORBIDDEN, identity.message, code=identity.code)

    store = secrets_store(request)
    password = await run_blocking(lambda: store.get_or_none(identity.secret_path)) or ""
    if not password:
        if identity.platform:
            raise ProblemError(
                503,
                "the platform ClickHouse reader has no credential yet",
                code="platform_reader_unprovisioned",
            )
        raise ProblemError(
            503,
            f"org '{identity.name}' has no ClickHouse credential yet",
            code="org_unprovisioned",
        )

    database = settings.clickhouse.effective_data_database
    return HyperDXConnection(
        name=identity.name,
        host=_ch_host_url(settings),
        username=identity.username,
        password=password,
        platform=identity.platform,
        database=database,
        tables=object_names(*_SEED_TABLE_IDS, data_database=database),
    )


class HyperDXTeamSource(BaseModel):
    """One Search source on one team, as dfe-hyperdx holds it."""

    id: str = Field(description="Search source id on that team")
    name: str = Field(description="DFE source name; the Search source carries the same one")
    table: dict[str, str] = Field(
        default_factory=dict,
        description="The ClickHouse table the source reads (databaseName, tableName)",
    )


class HyperDXTeamSources(BaseModel):
    """One Search team and the DFE sources it holds."""

    team: str = Field(description="Search team id")
    team_name: str = Field(description="Search team name: the ClickHouse user its members read as")
    sources: list[HyperDXTeamSource] = Field(default_factory=list)


class HyperDXSourcesResponse(BaseModel):
    """Where every deployed DFE source actually landed in Search."""

    teams: list[HyperDXTeamSources] = Field(default_factory=list)


def _require_every_org_reader(request: Request, user: CurrentUser) -> None:
    """Refuse a caller held to its orgs: a team is an org's ClickHouse user, so the listing spans them all."""
    if held_source_orgs(request, user) is not None:
        raise ProblemError(
            status.HTTP_403_FORBIDDEN,
            "This listing spans every org, and the caller is held to its own",
            code="org_not_granted",
        )


# Platform-wide: it lists every org's team, so it stays at system scope and refuses a held caller.
@router.get(
    "/sources",
    response_model=HyperDXSourcesResponse,
    summary="List Search Sources",
    dependencies=[
        Depends(require_action(scopes_dict["source_read"])),
        Depends(_require_every_org_reader),
    ],
)
async def hyperdx_sources(request: Request) -> HyperDXSourcesResponse:
    """List every Search team and the DFE sources on it.

    A deploy writes its source to every team, so this is the read that says where
    it landed. 503 when Search is not deployed or not answering -- an empty list
    would read as "the source is missing", which is a different fault.
    """
    client = getattr(request.app.state, "hyperdx_client", None)
    if client is None:
        raise ProblemError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "this deployment does not include Search",
            code="hyperdx_absent",
        )

    from dfe_engine.hyperdx.sources import list_sources_by_team

    listing = await list_sources_by_team(client)
    if listing is None:
        raise ProblemError(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Search did not answer", code="hyperdx_unreachable"
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
