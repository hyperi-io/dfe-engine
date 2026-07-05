"""Shared FastAPI dependencies for DFE Engine API.

Registry singletons are initialized in the lifespan handler and resolved
per-request via ``Depends()``.  Authentication checks four paths in order:

1. OIDC headers (X-Oidc-Subject) — production, Envoy Gateway fronted
2. API key (X-API-Key) — machine-to-machine
3. JWT Bearer — standalone/Docker users
4. Auth disabled — dev/test default, root context
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, NamedTuple

from fastapi import Depends, HTTPException, Request, status
from scalo.logger import logger

from dfe_engine.auth import AuthContext, AuthorizationError, Scope, ScopedGrant, authorize
from dfe_engine.auth.api_keys import APIKeyStore
from dfe_engine.auth.audit import (
    audit_login_denied,
    audit_login_success,
    audit_permission_denied,
)
from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.models import AuthzResult
from dfe_engine.auth.roles import RoleConfig
from dfe_engine.settings import DFESettings

# ── Settings ──────────────────────────────────────────────────


def get_app_settings(request: Request) -> DFESettings:
    """Get settings from app state (set in create_app)."""
    return request.app.state.settings


Settings = Annotated[DFESettings, Depends(get_app_settings)]


# ── Registry lifecycle ────────────────────────────────────────

_registries: dict[str, Any] = {}


def bootstrap_registries(settings: DFESettings) -> None:
    """Initialize singleton registries on startup. Called from lifespan."""
    if settings.schemas.schemas_dir:
        from dfe_engine.schema.registry import SchemaRegistry

        _registries["meta_schema"] = SchemaRegistry(schemas_directory=settings.schemas.schemas_dir)

    if settings.source.sources_dir:
        from dfe_engine.source.registry import SourceRegistry

        _registries["source"] = SourceRegistry(sources_directory=settings.source.sources_dir)

    if settings.services.config_yaml_dir:
        from dfe_engine.services.registry import ServiceConfigRegistry

        _registries["service_config"] = ServiceConfigRegistry(
            config_directory=settings.services.config_yaml_dir
        )

    if settings.fieldmap.fieldmaps_dir:
        from dfe_engine.fieldmap.registry import FieldMapRegistry

        _registries["fieldmap"] = FieldMapRegistry(
            field_maps_directory=settings.fieldmap.fieldmaps_dir
        )

    if settings.hunts.rules_dir:
        from dfe_engine.hunts.rule_registry import RuleRegistry

        _registries["rules"] = RuleRegistry(rules_directory=settings.hunts.rules_dir)

    if settings.hunts.hunt_dir:
        from dfe_engine.hunts.hunt_config_registry import HuntConfigRegistry

        hunt_dir = settings.hunts.hunt_dir.split(",")[0].strip()
        if hunt_dir:
            _registries["hunt_configs"] = HuntConfigRegistry(hunts_directory=hunt_dir)

    if settings.hunts.alert_destinations_dir:
        from scalo.config import DirectoryConfigStore

        _registries["alert_destinations"] = DirectoryConfigStore(
            directory=settings.hunts.alert_destinations_dir,
            writable=True,
        )

    if settings.deployment.config_dir:
        from dfe_engine.deployment.registry import DeploymentConfigRegistry

        _registries["deployment"] = DeploymentConfigRegistry(
            config_directory=settings.deployment.config_dir
        )


def shutdown_registries() -> None:
    """Cleanup registries on shutdown. Called from lifespan."""
    for _name, reg in list(_registries.items()):
        if hasattr(reg, "close"):
            reg.close()
    _registries.clear()


def _registry_dep(key: str, hint: str):
    """Build the 'resolve _registries[key] singleton or raise 503 not_configured'
    FastAPI dependency shared by every registry getter.

    The getters differed ONLY by the dict key and the not_configured hint, so the
    lookup-or-503 body lives here once. Each getter below is a distinct closure
    bound to the SAME module-level name the ``Annotated`` aliases + ``Depends()``
    call sites already reference, so nothing downstream changes (and each closure
    is its own object, so FastAPI still keys/caches them independently).
    """

    def _get():
        reg = _registries.get(key)
        if reg is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "not_configured", "message": hint},
            )
        return reg

    return _get


get_schema_registry = _registry_dep(
    "meta_schema",
    "SchemaRegistry not initialized — set DFE_SCHEMAS_DIR (schemas.schemas_dir)",
)
get_source_registry = _registry_dep(
    "source",
    "SourceRegistry not initialized — set DFE_SOURCES_DIR",
)
get_service_config_registry = _registry_dep(
    "service_config",
    "ServiceConfigRegistry not initialized — set DFE_SERVICES_CONFIG_YAML_DIR",
)
get_field_map_registry = _registry_dep(
    "fieldmap",
    "FieldMapRegistry not initialized — set DFE_FIELDMAPS_DIR",
)
get_alert_destinations_store = _registry_dep(
    "alert_destinations",
    "Alert destinations not initialized — set DFE_HUNTS_ALERT_DESTINATIONS_DIR",
)
get_deployment_config_registry = _registry_dep(
    "deployment",
    "DeploymentConfigRegistry not initialized — set DFE_DEPLOYMENT_CONFIG_DIR",
)
get_rule_registry = _registry_dep(
    "rules",
    "RuleRegistry not initialized — set DFE_HUNTS_RULES_DIR (hunts.rules_dir)",
)
get_hunt_config_registry = _registry_dep(
    "hunt_configs",
    "HuntConfigRegistry not initialized — set DFE_HUNTS_DIR (hunts.hunt_dir)",
)


def get_alert_destinations_store_optional():
    """Optional alert destinations store (for hunt delete cascade)."""
    return _registries.get("alert_destinations")


SchemaReg = Annotated[Any, Depends(get_schema_registry)]
SourceReg = Annotated[Any, Depends(get_source_registry)]
ServiceConfigReg = Annotated[Any, Depends(get_service_config_registry)]
FieldMapReg = Annotated[Any, Depends(get_field_map_registry)]
AlertDestStore = Annotated[Any, Depends(get_alert_destinations_store)]
OptionalAlertDestStore = Annotated[Any | None, Depends(get_alert_destinations_store_optional)]
DeploymentConfigReg = Annotated[Any, Depends(get_deployment_config_registry)]
RuleReg = Annotated[Any, Depends(get_rule_registry)]
HuntConfigReg = Annotated[Any, Depends(get_hunt_config_registry)]


# ── ClickHouse client ─────────────────────────────────────────


def get_clickhouse_client(settings: Settings) -> Any:
    """FastAPI dependency: resolve a pooled ClickHouse client wrapper.

    Returns a ``ClickHouseClientWrapper`` whose ``execute()`` accepts
    clickhouse-connect ``parameters={...}`` server-side binding. Tests
    override this via ``app.dependency_overrides`` to inject a fake client.
    """
    from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
    from dfe_engine.settings import get_clickhouse_config

    manager = ClickHouseManager.get_instance(get_clickhouse_config(settings))
    return manager.get_clickhouse_client()


ClickHouseClient = Annotated[Any, Depends(get_clickhouse_client)]


# ── Authentication ────────────────────────────────────────────


def _get_client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


class GroupResolution(NamedTuple):
    """Roles, org memberships, and scoped grants resolved from groups."""

    roles: list[str]
    org_ids: list[str]
    grants: list[ScopedGrant]


def _resolve_group_grants(
    groups: list[str],
    group_store: GroupStore,
) -> GroupResolution:
    """Resolve roles, org_ids, and scoped grants from a list of group names.

    Looks up each group in the GroupStore. Unknown groups are silently
    skipped (no error — the user just gets fewer roles). A system group's
    roles bind at system scope; an org-scoped group's roles bind at that
    org's scope only. org_ids collects the caller's org memberships (the
    owning org of each org-scoped group, plus each group's org_ids list).
    """
    roles: set[str] = set()
    org_ids: set[str] = set()
    grants: list[ScopedGrant] = []
    seen_grants: set[tuple[str, str]] = set()
    for group_name in groups:
        group = group_store.get(group_name)
        if group is None:
            continue
        scope_org = group.scope_org
        scope = Scope(type="org", id=scope_org) if scope_org else Scope()
        if scope_org:
            org_ids.add(scope_org)
        org_ids.update(group.org_ids)
        for role in group.roles:
            roles.add(role)
            key = (role, str(scope))
            if key not in seen_grants:
                seen_grants.add(key)
                grants.append(ScopedGrant(role=role, scope=scope))
    return GroupResolution(sorted(roles), sorted(org_ids), grants)


def _resolve_roles_from_groups(
    groups: list[str],
    group_store: GroupStore,
) -> tuple[list[str], list[str]]:
    """Resolve (roles, org_ids) from group names — see _resolve_group_grants."""
    resolution = _resolve_group_grants(groups, group_store)
    return resolution.roles, resolution.org_ids


def _merge_group_resolutions(*resolutions: GroupResolution) -> GroupResolution:
    """Union roles, org_ids, and scoped grants across several resolutions.

    Used by the OIDC path to combine the IdP-header groups with the acting
    account's STORE-side group memberships: each side contributes, neither
    replaces the other. Grants dedupe by (role, scope) so a role bound at the
    same scope by both sides lands once.
    """
    roles: set[str] = set()
    org_ids: set[str] = set()
    grants: list[ScopedGrant] = []
    seen_grants: set[tuple[str, str]] = set()
    for resolution in resolutions:
        roles.update(resolution.roles)
        org_ids.update(resolution.org_ids)
        for grant in resolution.grants:
            key = (grant.role, str(grant.scope))
            if key not in seen_grants:
                seen_grants.add(key)
                grants.append(grant)
    return GroupResolution(sorted(roles), sorted(org_ids), grants)


def _groups_for_local_account(request: Request, user_id: str) -> list[str]:
    """Load group names from AccountStore for JWT users (legacy tokens without groups claim)."""
    if user_id.startswith("apikey:"):
        return []
    account_store = getattr(request.app.state, "account_store", None)
    if account_store is None:
        return []
    account = account_store.get(user_id)
    return list(account.groups) if account is not None else []


def get_role_config(request: Request) -> RoleConfig:
    """Load the current role definitions from disk (not a stale in-memory snapshot)."""
    role_store = getattr(request.app.state, "role_store", None)
    if role_store is not None:
        try:
            return role_store.load_config()
        except FileNotFoundError:
            pass
    role_config = getattr(request.app.state, "role_config", None)
    if role_config is not None:
        return role_config
    return RoleConfig.load_builtin()


def _groups_from_stores(request: Request, user_id: str) -> list[str]:
    """Group names from GroupStore membership and AccountStore (never JWT)."""
    group_store: GroupStore | None = getattr(request.app.state, "group_store", None)
    if group_store is not None:
        from_membership = sorted(g.name for g in group_store.list() if user_id in g.members)
        if from_membership:
            return from_membership

    account_store = getattr(request.app.state, "account_store", None)
    if account_store is not None:
        account = account_store.get(user_id)
        if account is not None:
            return list(account.groups)
    return []


def _has_local_account(request: Request, user_id: str) -> bool:
    if user_id.startswith("apikey:"):
        return False
    account_store = getattr(request.app.state, "account_store", None)
    return account_store is not None and account_store.get(user_id) is not None


def resolve_live_grants_for_user(
    request: Request,
    user_id: str,
    *,
    fallback_groups: list[str] | None = None,
) -> GroupResolution:
    """Resolve roles/org_ids/grants from group membership (ignores JWT role claims)."""
    group_store: GroupStore | None = getattr(request.app.state, "group_store", None)
    if group_store is None:
        return GroupResolution([], [], [])

    if user_id.startswith("apikey:"):
        key_name = user_id.removeprefix("apikey:")
        api_key_store: APIKeyStore | None = getattr(request.app.state, "api_key_store", None)
        groups: list[str] = []
        if api_key_store is not None:
            key = api_key_store.get(key_name)
            if key is not None:
                groups = list(key.groups)
        if not groups:
            groups = list(fallback_groups or [])
        return _resolve_group_grants(groups, group_store)

    groups = sorted(g.name for g in group_store.list() if user_id in g.members)
    if not groups:
        groups = _groups_from_stores(request, user_id)
    if not groups and not _has_local_account(request, user_id):
        groups = list(fallback_groups or [])
    return _resolve_group_grants(groups, group_store)


def resolve_live_roles_for_user(
    request: Request,
    user_id: str,
    *,
    fallback_groups: list[str] | None = None,
) -> list[str]:
    """Resolve roles from group membership and role mappings (ignores JWT role claims)."""
    return resolve_live_grants_for_user(request, user_id, fallback_groups=fallback_groups).roles


def resolve_live_groups_for_user(
    request: Request,
    user_id: str,
    *,
    fallback_groups: list[str] | None = None,
) -> list[str]:
    """Return current group names for a user from stores."""
    if user_id.startswith("apikey:"):
        key_name = user_id.removeprefix("apikey:")
        api_key_store: APIKeyStore | None = getattr(request.app.state, "api_key_store", None)
        if api_key_store is not None:
            key = api_key_store.get(key_name)
            if key is not None:
                return list(key.groups)
        return list(fallback_groups or [])

    groups = _groups_from_stores(request, user_id)
    if groups:
        return groups
    if _has_local_account(request, user_id):
        return []
    return list(fallback_groups or [])


def require_local_account_enabled(request: Request, user_id: str) -> None:
    """Reject JWT auth when the backing local account exists and is disabled.

    Skips ``apikey:…`` subjects and usernames with no account record.
    """
    if user_id.startswith("apikey:"):
        return

    account_store = getattr(request.app.state, "account_store", None)
    if account_store is None:
        return

    account = account_store.get(user_id)
    if account is None or account.enabled:
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "unauthorized", "message": "Account disabled"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_oidc_account_enabled(request: Request, oidc_subject: str) -> None:
    """Reject an OIDC principal whose shadow account has been disabled.

    Mirror of require_local_account_enabled for the OIDC-header path: an admin
    disabling an external user via ``PUT /accounts/{key}`` (enabled=false) must
    take effect for OIDC logins too, not just JWT ones. The shadow account is
    keyed by JitProvisioner.account_key(subject) (NOT the raw subject), so we
    resolve it by that key. A brand-new JIT account is created enabled, so first
    login still passes; only an explicitly-disabled account is rejected.
    """
    from dfe_engine.auth.jit import JitProvisioner

    account_store = getattr(request.app.state, "account_store", None)
    if account_store is None:
        return

    account = account_store.get(JitProvisioner.account_key(oidc_subject))
    if account is None or account.enabled:
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "unauthorized", "message": "Account disabled"},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(request: Request) -> AuthContext:
    """Authenticate the request via one of four paths (checked in order).

    1. OIDC headers (X-Oidc-Subject) — set by Envoy Gateway
    2. API key (X-API-Key) — machine-to-machine
    3. JWT Bearer token — standalone/Docker users
    4. Auth disabled — dev/test root context

    When ``auth.enabled=False`` (dev/test default), returns a root AuthContext
    that bypasses authorization if no credentials are provided.
    """
    settings: DFESettings = request.app.state.settings
    request_id = request.headers.get("X-Request-ID")
    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("User-Agent")

    # ── Path 1: OIDC headers (Envoy Gateway) ────────────────────
    oidc_subject = request.headers.get("X-Oidc-Subject")
    if oidc_subject:
        group_store: GroupStore = request.app.state.group_store
        oidc_email = request.headers.get("X-Oidc-Email") or None
        raw_groups = request.headers.get("X-Oidc-Groups", "")
        groups = [g.strip() for g in raw_groups.split(",") if g.strip()]
        # IdP-header groups are the live signal the IdP asserts for this login.
        header_resolution = _resolve_group_grants(groups, group_store)

        # JIT provisioning -- create/refresh the shadow account on OIDC login.
        # First login also joins the account to its `org_<domain>` org group, so
        # this must run BEFORE we resolve the account's store-side memberships.
        jit = getattr(request.app.state, "jit_provisioner", None)
        if jit:
            try:
                jit.ensure_account(oidc_subject, groups, "oidc")
            except Exception:
                logger.exception("JIT provisioning failed", user_id=oidc_subject)

        # Consistency with the local/JWT paths: an external user's DFE-managed
        # group memberships -- the JIT `org_<domain>` org group carrying the org's
        # org_ids + role org_analyst, or any group an admin added the shadow
        # account to -- live in the STORE, not the IdP header, so header groups
        # alone never surface them and the user stays un-tenant-scoped despite the
        # association. Resolve them via the SAME helper the JWT path uses, keyed by
        # the shadow-account key (account_key(subject)) under which the account
        # joins those groups, and UNION with the header grants so BOTH contribute
        # (neither replaces the other).
        from dfe_engine.auth.jit import JitProvisioner

        store_resolution = resolve_live_grants_for_user(
            request, JitProvisioner.account_key(oidc_subject), fallback_groups=groups
        )
        resolution = _merge_group_resolutions(header_resolution, store_resolution)
        roles, org_ids = resolution.roles, resolution.org_ids
        logger.debug("OIDC auth", user_id=oidc_subject, groups=groups, roles=roles)
        audit_login_success(oidc_subject, "oidc", client_ip, roles)

        # The account.enabled flag must gate OIDC principals too, not only JWT
        # ones -- otherwise disabling an external user has no effect. Checked
        # AFTER ensure_account so a freshly provisioned (enabled) account passes.
        require_oidc_account_enabled(request, oidc_subject)

        return AuthContext(
            user_id=oidc_subject,
            email=oidc_email,
            roles=roles,
            grants=resolution.grants,
            groups=groups,
            org_ids=org_ids,
            request_id=request_id,
            client_ip=client_ip,
            user_agent=user_agent,
        )

    # ── Path 2: API key ─────────────────────────────────────────
    api_key_header = request.headers.get("X-API-Key")
    if api_key_header:
        api_key_store: APIKeyStore = request.app.state.api_key_store
        key_meta = api_key_store.verify(api_key_header)
        if key_meta is None:
            audit_login_denied(
                api_key_header[:16] + "...",
                "api_key",
                client_ip,
                "invalid_key",
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "unauthorized", "message": "Invalid API key"},
            )
        group_store = request.app.state.group_store
        resolution = _resolve_group_grants(key_meta.groups, group_store)
        roles, org_ids = resolution.roles, resolution.org_ids
        logger.debug(
            "API key auth",
            key_name=key_meta.name,
            groups=key_meta.groups,
            roles=roles,
        )
        audit_login_success(f"apikey:{key_meta.name}", "api_key", client_ip, roles)
        return AuthContext(
            user_id=f"apikey:{key_meta.name}",
            roles=roles,
            grants=resolution.grants,
            groups=key_meta.groups,
            org_ids=org_ids,
            request_id=request_id,
            client_ip=client_ip,
            user_agent=user_agent,
        )

    # ── Path 3: JWT Bearer token ────────────────────────────────
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]

        import jwt
        from jwt.exceptions import InvalidTokenError

        try:
            payload = jwt.decode(
                token,
                settings.api.jwt_secret,
                algorithms=[settings.api.jwt_algorithm],
            )
        except InvalidTokenError as e:
            audit_login_denied("unknown", "jwt", client_ip, str(e))
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "unauthorized", "message": f"Invalid token: {e}"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        jwt_user_id = payload.get("sub", "")
        jwt_email = payload.get("email") or None
        jwt_groups = payload.get("groups")
        if jwt_groups is None:
            jwt_groups = _groups_for_local_account(request, jwt_user_id)
        elif not isinstance(jwt_groups, list):
            jwt_groups = []
        require_local_account_enabled(request, jwt_user_id)
        live = resolve_live_grants_for_user(request, jwt_user_id, fallback_groups=jwt_groups)
        live_groups = resolve_live_groups_for_user(request, jwt_user_id, fallback_groups=jwt_groups)
        # org_ids MUST come from LIVE membership only. The repository read-gate
        # authorises org reads via `scope_id in user.org_ids`, and /auth/refresh
        # copies org_ids back into the next token, so unioning the signed org_ids
        # claim let a removed org member keep reading that org indefinitely - a
        # self-perpetuating claim (F-REPO-ORGIDS). Mirror the roles path, which
        # already ignores JWT authz claims.
        org_ids = live.org_ids
        audit_login_success(jwt_user_id, "jwt", client_ip, live.roles)
        return AuthContext(
            org_id=payload.get("org_id", "default"),
            user_id=jwt_user_id,
            email=jwt_email,
            roles=live.roles,
            grants=live.grants,
            org_ids=org_ids,
            groups=live_groups,
            request_id=request_id,
            client_ip=client_ip,
            user_agent=user_agent,
        )

    # ── Path 4: Auth disabled (dev/test) ────────────────────────
    if not settings.auth.enabled:
        return AuthContext(org_id="default", user_id="dev", roles=["admin"])

    audit_login_denied("anonymous", "none", client_ip, "no_credentials")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "unauthorized", "message": "Authentication required"},
        headers={"WWW-Authenticate": "Bearer"},
    )


CurrentUser = Annotated[AuthContext, Depends(get_current_user)]


# ── Tenant-scoped ClickHouse client (direct-read endpoints) ───


def get_tenant_scoped_clickhouse_client(request: Request, user: CurrentUser) -> Any:
    """FastAPI dependency: the privilege-appropriate CH client for THIS principal.

    Resolves the acting user's fixed CH user via the ConnectionRegistry so a
    DIRECT-CH read (sampler, discovery) runs under the right identity, correctly
    scoped:

      * org_analyst -> the row-filtered ``dfe_tenant_reader`` wrapped in a
        TenantScopedClient injecting ``DFE_current_tenant_id`` = the caller's
        org_ids (empty -> '' -> zero rows, fail closed);
      * data_analyst_ro / data_viewer / infra_ro -> the read-only ``dfe_analyst_ro``
        (drops readonly-incompatible per-query settings; no tenant setting);
      * admin / infra / data_analyst -> their fixed user as-is (unrestricted).

    This REPLACES ``ClickHouseManager.get_instance`` (the process-wide ADMIN
    singleton) for the direct-CH-read endpoints, so an org-scoped principal can
    never read another org's rows. The parameterized-VIEW execute path keeps its
    OWN server-injected ``org_id`` view parameter and is deliberately unchanged.
    Host/port come from settings (seeded into the registry at bootstrap). Tests
    override this via ``app.dependency_overrides`` to inject a fake client.

    Raises:
        HTTPException: 503 when the registry is unconfigured or CH is unreachable.
    """
    registry = getattr(request.app.state, "connection_registry", None)
    if registry is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_configured", "message": "ClickHouse connection not configured"},
        )
    try:
        return registry.read_client_for_user(user)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "connection_error", "message": f"Cannot connect to ClickHouse: {exc}"},
        ) from exc


TenantClient = Annotated[Any, Depends(get_tenant_scoped_clickhouse_client)]


# ── Authorization ─────────────────────────────────────────────

# The ONLY action an ORG-scoped grant may satisfy at the caller's OWN org. This is
# a deliberately minimal allowlist, NOT "all data-plane actions". scoped-rbac's
# tenant boundary is enforced solely at the ClickHouse row-policy layer, and the
# ONLY handler that routes through the caller's per-org connection (org_id
# force-injected + tenant_isolated guard, see query/executor.py) is the
# parameterized-view executor behind query:execute. An org-scoped grant therefore
# reaches ONLY that path, and the view returns just the caller's org rows.
#
# Everything else stays system-only - deliberately, not by omission - because the
# rest of the app operates on a SHARED, un-partitioned layer:
#   * config WRITES (source/schema/rule/hunt/fieldmap/alert :write/:delete) mutate
#     ONE global YAML registry shared by every org; an org grant must never satisfy
#     them or one tenant rewrites config for all orgs.
#   * config READS (source/schema/... :read) disclose that shared catalogue, which
#     can carry connection/destination secrets - kept system-only pending a
#     content-sensitivity + per-org-config decision.
#   * raw-data reads via the shared ADMIN ClickHouse client (sampler:read,
#     discovery:read, schema json-paths/sample-rows, and the /raw query path)
#     bypass row policies entirely - a cross-org DATA leak until they route through
#     the caller's per-org connection.
# Widening this set is a scoped-rbac RESOURCE-LAYER follow-up (org-partition the
# config, or route those reads per-org), NOT a one-line edit. Err toward lockout.
# NB: /raw was split off query:execute onto the admin-only query:raw action so that
# query:execute gates ONLY the org-isolated view executor here.
TENANT_ACTIONS: frozenset[str] = frozenset({"query:execute"})


def _authorize_resolved(
    user: AuthContext,
    action: str,
    scope: Scope | None,
    *,
    enabled: bool,
    role_config: RoleConfig | None,
) -> AuthzResult:
    """``authorize()`` with tenant-scope resolution.

    An explicit ``scope`` is honoured as-is. For a TENANT action with no explicit
    scope, try system then each org the caller belongs to, allowing if any grant
    covers it - so an org-scoped user reaches their OWN org's data plane while a
    system (unrestricted) user still passes and a cross-org request is denied.
    Every other unscoped action stays a single system check (historical behaviour).
    """
    if scope is not None or action not in TENANT_ACTIONS:
        return authorize(user, action, scope=scope, enabled=enabled, role_config=role_config)
    candidates: list[Scope | None] = [None]
    candidates += [
        Scope(type="org", id=org)
        for org in (user.org_ids or ([user.org_id] if user.org_id else []))
    ]
    result = AuthzResult(allowed=False, reason=f"no role grants '{action}' at the caller's scope")
    for cand in candidates:
        result = authorize(user, action, scope=cand, enabled=enabled, role_config=role_config)
        if result.allowed:
            return result
    return result


def is_action_allowed(
    request: Request,
    user: AuthContext,
    action: str,
    *,
    scope: Scope | None = None,
) -> bool:
    """Non-raising authorize() for visibility filtering in handlers."""
    settings: DFESettings = request.app.state.settings
    role_config = getattr(request.app.state, "role_config", None)
    return _authorize_resolved(
        user, action, scope, enabled=settings.auth.enabled, role_config=role_config
    ).allowed


def check_action(
    request: Request,
    user: AuthContext,
    action: str,
    *,
    scope: Scope | None = None,
) -> None:
    """Handler-level RBAC check for scopes only known at request time.

    Use this (instead of the require_action dependency) when the target
    scope comes from the resource being touched - e.g. the stored scope
    of a group, or an org name in the path. Raises AuthorizationError
    exactly like require_action.
    """
    settings: DFESettings = request.app.state.settings
    role_config = getattr(request.app.state, "role_config", None)
    result = _authorize_resolved(
        user, action, scope, enabled=settings.auth.enabled, role_config=role_config
    )
    if not result.allowed:
        audit_permission_denied(user.user_id, action, user.roles, result.reason)
        raise AuthorizationError(f"Action '{action}' denied: {result.reason}")


def require_action(action: str, *, scope: Scope | None = None):
    """FastAPI dependency factory for RBAC enforcement.

    ``scope=None`` is a system-scope check (the historical behaviour).
    Pass a Scope for endpoints whose target scope is static; use
    check_action() inside the handler when it depends on the request.

    Usage::

        @router.post("/sources")
        async def create_source(
            user: CurrentUser,
            _auth: None = Depends(require_action("source:write")),
        ): ...
    """

    async def _check(
        request: Request,
        user: AuthContext = Depends(get_current_user),
        settings: DFESettings = Depends(get_app_settings),
    ) -> None:
        role_config = getattr(request.app.state, "role_config", None)
        result = _authorize_resolved(
            user, action, scope, enabled=settings.auth.enabled, role_config=role_config
        )
        if not result.allowed:
            audit_permission_denied(user.user_id, action, user.roles, result.reason)
            raise AuthorizationError(f"Action '{action}' denied: {result.reason}")

    return _check


# ── JWT helpers ──────────────────────────────────────────────


def create_access_token(
    data: dict,
    settings: DFESettings,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a signed JWT access token."""
    import jwt

    to_encode = data.copy()
    expire = datetime.now(UTC) + (
        expires_delta or timedelta(minutes=settings.api.jwt_expire_minutes)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, settings.api.jwt_secret, algorithm=settings.api.jwt_algorithm)
