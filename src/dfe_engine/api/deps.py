"""Shared FastAPI dependencies for DFE Engine API.

Registry singletons are initialized in the lifespan handler and resolved
per-request via ``Depends()``.  Authentication checks four paths in order:

1. OIDC headers (X-Oidc-Subject) -- Envoy Gateway fronted; trusted ONLY
   when auth.trust_proxy_auth_headers is set (else ignored, fail closed)
2. API key (X-API-Key) -- machine-to-machine
3. JWT Bearer -- standalone/Docker users
4. Auth disabled -- dev/test default, root context
"""

from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Any, NamedTuple

from fastapi import Depends, HTTPException, Request, status
from scalo.logger import logger

from dfe_engine.api.password_change import refuse_until_password_changed
from dfe_engine.auth import AuthContext, AuthorizationError, Scope, ScopedGrant, authorize
from dfe_engine.auth.api_keys import APIKeyStore
from dfe_engine.auth.audit import (
    audit_login_denied,
    audit_permission_denied,
)
from dfe_engine.auth.groups import Group, GroupStore
from dfe_engine.auth.jit import (
    API_KEY_SUBJECT_PREFIX,
    JitAccountUnavailableError,
    JitIdentityCollisionError,
)
from dfe_engine.auth.roles import RoleConfig
from dfe_engine.settings import DFESettings, is_dev_posture

if TYPE_CHECKING:
    from dfe_engine.auth.jwt_authority import JwtAuthority

# -- Settings --------------------------------------------------


def get_app_settings(request: Request) -> DFESettings:
    """Get settings from app state (set in create_app)."""
    return request.app.state.settings


Settings = Annotated[DFESettings, Depends(get_app_settings)]


# -- Registry lifecycle ----------------------------------------

_registries: dict[str, Any] = {}


def bootstrap_registries(
    settings: DFESettings, gitcrud: Any | None = None, forge: Any | None = None
) -> None:
    """Initialize singleton registries on startup. Called from lifespan.

    ``gitcrud`` (the Governed Ops engine over the deploy repo, when gitops is
    enabled) makes the deploy repo the SSoT for sources (``config/sources/``),
    hunts (``config/hunts/``) and rules (``config/rules/``); without it each
    registry falls back to its plain YAML directory. ``forge`` opens the review
    PR when the deployment posture refuses a direct commit.
    """
    if settings.schemas.schemas_dir:
        from dfe_engine.schema.registry import SchemaRegistry

        _registries["meta_schema"] = SchemaRegistry(schemas_directory=settings.schemas.schemas_dir)

    # A derived schema is a deployment's own artefact, so it follows the sources
    # into the deploy repo when gitops is on and sits under the schemas tree
    # otherwise.
    if gitcrud is not None or settings.schemas.schemas_dir:
        from dfe_engine.schema.derived_registry import DerivedSchemaRegistry

        _registries["derived_schema"] = DerivedSchemaRegistry.from_settings(settings, crud=gitcrud)

    if gitcrud is not None or settings.source.sources_dir:
        from dfe_engine.source.registry import SourceRegistry

        _registries["source"] = SourceRegistry(
            sources_directory=settings.source.sources_dir or None,
            crud=gitcrud,
        )

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

    # The hunt runner reads hunts and rules off a git-sync of the deploy repo, so
    # with gitops on they are governed there rather than in a directory only the
    # engine pod can see (dfe-infra#212).
    from dfe_engine.hunts.deploy_repo import deploy_store

    if gitcrud is not None or settings.hunts.rules_dir:
        from dfe_engine.hunts.rule_registry import RuleRegistry

        _registries["rules"] = RuleRegistry(
            rules_directory=settings.hunts.rules_dir or None,
            deploy_repo=deploy_store(gitcrud, "rules", settings=settings, forge=forge),
        )

    hunt_dir = settings.hunts.hunt_dir.split(",")[0].strip()
    if gitcrud is not None or hunt_dir:
        from dfe_engine.hunts.hunt_config_registry import HuntConfigRegistry

        _registries["hunt_configs"] = HuntConfigRegistry(
            hunts_directory=hunt_dir or None,
            deploy_repo=deploy_store(gitcrud, "hunts", settings=settings, forge=forge),
        )

    if settings.hunts.alert_destinations_dir:
        from dfe_engine.hunts.alert import AlertDestinationRegistry

        _registries["alert_destinations"] = AlertDestinationRegistry(
            directory=settings.hunts.alert_destinations_dir,
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


def get_schema_registry():
    """FastAPI dependency: resolve SchemaRegistry singleton (meta schemas)."""
    reg = _registries.get("meta_schema")
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "SchemaRegistry not initialized -- set DFE_SCHEMAS_DIR (schemas.schemas_dir)",
            },
        )
    return reg


def get_derived_schema_registry():
    """FastAPI dependency: resolve the DerivedSchemaRegistry singleton."""
    reg = _registries.get("derived_schema")
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": (
                    "DerivedSchemaRegistry not initialized -- set DFE_SCHEMAS_DIR "
                    "(schemas.schemas_dir) or enable gitops"
                ),
            },
        )
    return reg


def get_source_registry():
    """FastAPI dependency: resolve SourceRegistry singleton."""
    reg = _registries.get("source")
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "SourceRegistry not initialized -- set DFE_SOURCES_DIR",
            },
        )
    return reg


def get_source_registry_optional():
    """Optional SourceRegistry (for callers that must work without one configured)."""
    return _registries.get("source")


def get_service_config_registry():
    """FastAPI dependency: resolve ServiceConfigRegistry singleton."""
    reg = _registries.get("service_config")
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "ServiceConfigRegistry not initialized"
                " -- set DFE_SERVICES_CONFIG_YAML_DIR",
            },
        )
    return reg


def get_field_map_registry():
    """FastAPI dependency: resolve FieldMapRegistry singleton."""
    reg = _registries.get("fieldmap")
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "FieldMapRegistry not initialized -- set DFE_FIELDMAPS_DIR",
            },
        )
    return reg


def get_alert_destinations_registry():
    """FastAPI dependency: resolve the AlertDestinationRegistry singleton."""
    reg = _registries.get("alert_destinations")
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "Alert destinations not initialized"
                " -- set DFE_HUNTS_ALERT_DESTINATIONS_DIR",
            },
        )
    return reg


def get_alert_destinations_registry_optional():
    """Optional AlertDestinationRegistry (for hunt delete cascade)."""
    return _registries.get("alert_destinations")


def get_deployment_config_registry():
    """FastAPI dependency: resolve DeploymentConfigRegistry singleton."""
    reg = _registries.get("deployment")
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "DeploymentConfigRegistry not initialized"
                " -- set DFE_DEPLOYMENT_CONFIG_DIR",
            },
        )
    return reg


def get_rule_registry():
    """FastAPI dependency: resolve RuleRegistry singleton."""
    reg = _registries.get("rules")
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "RuleRegistry not initialized -- set DFE_HUNTS_RULES_DIR (hunts.rules_dir)",
            },
        )
    return reg


def get_hunt_config_registry():
    """FastAPI dependency: resolve HuntConfigRegistry singleton."""
    reg = _registries.get("hunt_configs")
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "HuntConfigRegistry not initialized -- set DFE_HUNTS_DIR (hunts.hunt_dir)",
            },
        )
    return reg


SchemaReg = Annotated[Any, Depends(get_schema_registry)]
DerivedSchemaReg = Annotated[Any, Depends(get_derived_schema_registry)]
SourceReg = Annotated[Any, Depends(get_source_registry)]
ServiceConfigReg = Annotated[Any, Depends(get_service_config_registry)]
FieldMapReg = Annotated[Any, Depends(get_field_map_registry)]
AlertDestRegistry = Annotated[Any, Depends(get_alert_destinations_registry)]
OptionalAlertDestRegistry = Annotated[Any | None, Depends(get_alert_destinations_registry_optional)]
DeploymentConfigReg = Annotated[Any, Depends(get_deployment_config_registry)]
RuleReg = Annotated[Any, Depends(get_rule_registry)]
HuntConfigReg = Annotated[Any, Depends(get_hunt_config_registry)]


# -- ClickHouse client -----------------------------------------


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


# -- Authentication --------------------------------------------


def _get_client_ip(request: Request) -> str | None:
    """Caller address for the audit trail.

    X-Forwarded-For is whatever the caller typed unless a trusted proxy rewrote
    it, so it is read only behind ``auth.trust_proxy_auth_headers`` - the same
    gate the X-Oidc-* identity headers sit behind.

    The fallback is NOT necessarily the socket peer: ProxyHeadersMiddleware is the
    app's outermost middleware, so where ``api.forwarded_allow_ips`` trusts the
    gateway it has already rewritten ``scope["client"]`` from X-Forwarded-For. So
    this returns the proxy-forwarded address behind a trusted proxy, and the socket
    peer otherwise - which is the address the audit trail wants either way, because
    behind a gateway the socket peer is only ever the gateway.
    """
    settings: DFESettings = request.app.state.settings
    if settings.auth.trust_proxy_auth_headers:
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
    """Resolve roles, org_ids, and scoped grants from a list of group identifiers.

    Each identifier is looked up by group NAME first, then by provider
    ``source_id`` (so a token carrying Entra GUIDs or Google group keys resolves
    against the sync-populated group files). Unknown identifiers are silently
    skipped (no error -- the user just gets fewer roles). A system group's roles
    bind at system scope; an org-scoped group's roles bind at that org's scope
    only. org_ids collects the caller's org memberships (the owning org of each
    org-scoped group, plus each group's org_ids list).
    """
    roles: set[str] = set()
    org_ids: set[str] = set()
    grants: list[ScopedGrant] = []
    seen_grants: set[tuple[str, str]] = set()
    # Providers that emit opaque group identifiers rather than names (Entra sends
    # object GUIDs, Google sends group keys) arrive here as those identifiers.
    # Resolve by name first - the common case, and what dex/okta/local all use -
    # then fall back to the sync-populated source_id index. The index is built
    # only when a name misses, so name-only workloads pay nothing for it.
    source_index: dict[str, Group] | None = None
    for group_name in groups:
        group = group_store.get(group_name)
        if group is None:
            if source_index is None:
                source_index = group_store.by_source_id()
            group = source_index.get(group_name)
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
    """Resolve (roles, org_ids) from group names -- see _resolve_group_grants."""
    resolution = _resolve_group_grants(groups, group_store)
    return resolution.roles, resolution.org_ids


def _groups_for_local_account(request: Request, user_id: str) -> list[str]:
    """Load group names from AccountStore for JWT users (legacy tokens without groups claim)."""
    if user_id.startswith(API_KEY_SUBJECT_PREFIX):
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
    if user_id.startswith(API_KEY_SUBJECT_PREFIX):
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

    if user_id.startswith(API_KEY_SUBJECT_PREFIX):
        key_name = user_id.removeprefix(API_KEY_SUBJECT_PREFIX)
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
    if user_id.startswith(API_KEY_SUBJECT_PREFIX):
        key_name = user_id.removeprefix(API_KEY_SUBJECT_PREFIX)
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


def account_for_session_subject(store: Any, user_id: str):
    """Look up the store account a session subject is bound to.

    Tries the raw subject first, then the JIT-sanitised stem an OIDC login
    writes, so a JWT ``sub`` of ``alice@example.com`` matches
    ``alice-example-com.yaml``. The stem reaches only an account an IdP owns (a
    JIT shadow or a SCIM record): a local credential answers to its own name
    alone, so ``Bob`` never binds the local ``bob``. An API-key subject binds no
    account. Several subjects sanitise to one stem, and a subject can be
    another's stem, so an account that records another subject is not this
    session's whichever lookup found it.
    """
    if user_id.startswith(API_KEY_SUBJECT_PREFIX):
        return None
    account = store.get(user_id)
    if account is not None:
        return None if account.subject and account.subject != user_id else account
    from dfe_engine.auth.jit import JitProvisioner

    stem = JitProvisioner.sanitise_username(user_id)
    if not stem or stem == user_id:
        return None
    shadow = store.get(stem)
    if shadow is None or not shadow.source_provider:
        return None
    if shadow.subject and shadow.subject != user_id:
        return None
    return shadow


def require_local_account_enabled(request: Request, user_id: str) -> Any:
    """Reject a session when the account it is bound to is disabled or blocked.

    The account is the one :func:`account_for_session_subject` binds, so a
    subject that binds none (an API key, a username with no record) passes. An
    account stored under the raw subject still refuses when disabled or blocked
    even where it records another subject and so is not bound.

    Returns:
        The session's store account, or None when it has none.
    """
    account_store = getattr(request.app.state, "account_store", None)
    if account_store is None:
        return None

    bound = account_for_session_subject(account_store, user_id)
    # Role resolution reads the account stored under the raw subject, bound or not.
    account = bound or account_store.get(user_id)
    if account is None:
        return None
    denied = account.session_denied()
    if denied is None:
        return bound
    code, message = denied
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": code, "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(request: Request) -> AuthContext:
    """Authenticate the request via one of four paths (checked in order).

    1. OIDC headers (X-Oidc-Subject) -- set by Envoy Gateway, trusted only
       when auth.trust_proxy_auth_headers is set (else ignored, fail closed)
    2. API key (X-API-Key) -- machine-to-machine
    3. JWT Bearer token -- standalone/Docker users
    4. Auth disabled -- dev/test root context

    When ``auth.enabled=False`` (dev/test default), returns a root AuthContext
    that bypasses authorization if no credentials are provided.
    """
    # No auth.login.success here: this runs on every request and creates no
    # session, so the login audit belongs to the credential exchange
    # (POST /auth/login, GET /auth/oidc/{provider}/callback).
    settings: DFESettings = request.app.state.settings
    request_id = request.headers.get("X-Request-ID")
    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("User-Agent")

    # -- Path 1: OIDC headers (Envoy Gateway) --------------------
    # SECURITY: X-Oidc-* are trusted ONLY when the deployment declares it runs
    # behind a trusted proxy that authenticates the user and injects them
    # (auth.trust_proxy_auth_headers). Unfronted, these headers are
    # client-spoofable -> auth bypass + privilege escalation, so when the gate
    # is off we ignore them (fail closed) and fall through to API-key / JWT.
    oidc_subject = request.headers.get("X-Oidc-Subject")
    if oidc_subject and settings.auth.trust_proxy_auth_headers:
        group_store: GroupStore = request.app.state.group_store
        oidc_email = request.headers.get("X-Oidc-Email") or None
        raw_groups = request.headers.get("X-Oidc-Groups", "")
        groups = [g.strip() for g in raw_groups.split(",") if g.strip()]
        resolution = _resolve_group_grants(groups, group_store)
        roles, org_ids = resolution.roles, resolution.org_ids
        logger.debug("OIDC auth", user_id=oidc_subject, groups=groups, roles=roles)

        # JIT provisioning -- create shadow account on first OIDC login
        jit = getattr(request.app.state, "jit_provisioner", None)
        if jit:
            try:
                # The configured provider, not the protocol: a deployment running
                # this path AND the RP callback must stamp one name for one IdP,
                # or the guard reads its second path as another identity.
                jit.ensure_account(
                    oidc_subject,
                    groups,
                    settings.auth.proxy_provider,
                    email=oidc_email or "",
                )
            except JitIdentityCollisionError as exc:
                # Ordered before the catch-all: a refused identity must reach the
                # caller as a 401, never be logged and waved through with a token.
                audit_login_denied(oidc_subject, "oidc", client_ip, exc.reason)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={"code": "unauthorized", "message": str(exc)},
                    headers={"WWW-Authenticate": "Bearer"},
                ) from exc
            except JitAccountUnavailableError as exc:
                audit_login_denied(oidc_subject, "oidc", client_ip, exc.reason)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={"code": exc.reason, "message": str(exc)},
                    headers={"WWW-Authenticate": "Bearer"},
                ) from exc
            except Exception:
                logger.exception("JIT provisioning failed", user_id=oidc_subject)

        require_local_account_enabled(request, oidc_subject)
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

    # -- Path 2: API key -----------------------------------------
    api_key_header = request.headers.get("X-API-Key")
    if api_key_header:
        api_key_store: APIKeyStore = request.app.state.api_key_store
        key_meta, reason = api_key_store.verify_detailed(api_key_header)
        if key_meta is None:
            audit_login_denied(
                api_key_header[:16] + "...",
                "api_key",
                client_ip,
                reason,
            )
            # disabled_key/expired_key are only reported once the hash check has
            # proven the caller holds the real key, so the specific message
            # tells them nothing they did not already have.
            message = {
                "expired_key": "API key expired",
                "disabled_key": "API key disabled",
            }.get(reason, "Invalid API key")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "unauthorized", "message": message},
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
        return AuthContext(
            user_id=f"{API_KEY_SUBJECT_PREFIX}{key_meta.name}",
            roles=roles,
            grants=resolution.grants,
            groups=key_meta.groups,
            org_ids=org_ids,
            request_id=request_id,
            client_ip=client_ip,
            user_agent=user_agent,
        )

    # -- Path 3: JWT Bearer token --------------------------------
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]

        from jwt.exceptions import InvalidTokenError

        try:
            payload = request.app.state.jwt_authority.verify(token)
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
        account = require_local_account_enabled(request, jwt_user_id)
        refuse_until_password_changed(request, account)
        if account is not None and account.password_change_required:
            # Its token carries no roles, groups or orgs, so neither does the session.
            return AuthContext(
                org_id=payload.get("org_id", "default"),
                user_id=jwt_user_id,
                email=jwt_email,
                request_id=request_id,
                client_ip=client_ip,
                user_agent=user_agent,
            )
        live = resolve_live_grants_for_user(request, jwt_user_id, fallback_groups=jwt_groups)
        live_groups = resolve_live_groups_for_user(request, jwt_user_id, fallback_groups=jwt_groups)
        claim_org_ids = payload.get("org_ids", [])
        org_ids = sorted(set(claim_org_ids) | set(live.org_ids)) if claim_org_ids else live.org_ids
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

    # -- Path 4: Auth disabled (dev/test) ------------------------
    # Gated on the POSTURE as well as the flag. `auth.enabled` alone handed an
    # anonymous request roles=["admin"], and `env` defaults to "production" while
    # `auth.enabled` defaults to False, so the pairing that grants anonymous
    # admin was the out-of-the-box one. DFESettings rejects it at load now; this
    # is the second line, for anything holding a settings object that did not
    # come through that validator.
    if not settings.auth.enabled and is_dev_posture(settings.env):
        return AuthContext(org_id="default", user_id="dev", roles=["admin"])

    audit_login_denied("anonymous", "none", client_ip, "no_credentials")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "unauthorized", "message": "Authentication required"},
        headers={"WWW-Authenticate": "Bearer"},
    )


CurrentUser = Annotated[AuthContext, Depends(get_current_user)]


# -- Authorization ---------------------------------------------


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
    return authorize(
        user,
        action,
        scope=scope,
        enabled=settings.auth.enabled,
        role_config=role_config,
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
    result = authorize(
        user,
        action,
        scope=scope,
        enabled=settings.auth.enabled,
        role_config=role_config,
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
        result = authorize(
            user,
            action,
            scope=scope,
            enabled=settings.auth.enabled,
            role_config=role_config,
        )
        if not result.allowed:
            audit_permission_denied(user.user_id, action, user.roles, result.reason)
            raise AuthorizationError(f"Action '{action}' denied: {result.reason}")

    return _check


# -- JWT helpers ----------------------------------------------


# One JwtAuthority per distinct (secrets + jwt) config, reused: the app's
# app.state.jwt_authority and create_access_token resolve the SAME instance (hence
# the same signing key + kid), so a token minted here verifies at the app.
_authority_cache: dict[tuple, JwtAuthority] = {}


def jwt_authority_for(settings: DFESettings) -> JwtAuthority:
    """Get (or build) the ES384 JWT authority for these settings."""
    from dfe_engine.auth.jwt_authority import JwtAuthority
    from dfe_engine.secrets import build_secrets

    s = settings.secrets
    a = settings.api
    cache_key = (
        s.provider,
        s.path,
        s.addr,
        s.mount,
        s.role,
        a.jwt_issuer,
        a.jwt_algorithm,
        a.jwt_key_path,
    )
    authority = _authority_cache.get(cache_key)
    if authority is None:
        authority = JwtAuthority(
            build_secrets(s),
            issuer=a.jwt_issuer,
            algorithm=a.jwt_algorithm,
            key_path=a.jwt_key_path,
            expire_minutes=a.jwt_expire_minutes,
        )
        _authority_cache[cache_key] = authority
    return authority


def create_access_token(
    data: dict,
    settings: DFESettings,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a signed ES384 DFE identity token (via the JWT authority)."""
    return jwt_authority_for(settings).sign(data, expires_delta=expires_delta)
