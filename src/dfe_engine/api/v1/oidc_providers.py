#  Project:      dfe-engine
#  File:         api/v1/oidc_providers.py
#  Purpose:      OIDC provider CRUD REST endpoints (admin only)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""OIDC provider management router -- CRUD, sync, and connectivity test.

POST   /api/v1/auth/oidc-providers                        -> Create provider
GET    /api/v1/auth/oidc-providers                        -> List providers
GET    /api/v1/auth/oidc-providers/{name}                 -> Get provider config
PUT    /api/v1/auth/oidc-providers/{name}                 -> Update provider
DELETE /api/v1/auth/oidc-providers/{name}                 -> Detach provider
POST   /api/v1/auth/oidc-providers/{name}/sync            -> Force group sync
GET    /api/v1/auth/oidc-providers/{name}/test             -> Test directory connectivity
GET    /api/v1/auth/oidc-providers/{name}/verify-login      -> Verify login config

All endpoints require admin role (org:write).

Credentials go IN as values and are written straight to the DfeSecrets interface. The
provider YAML keeps only the store PATH. A response carries the client id, the
secret paths and the env var names, never a secret value.
"""

import functools
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal, NamedTuple

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, field_validator
from scalo.api import (
    ListQuery,
    Page,
    ProblemError,
    list_query,
    paginate,
    scalo_extra,
    sort_by_order,
)
from scalo.concurrency import run_blocking
from scalo.logger import logger

from dfe_engine.api.conventions import apply_search
from dfe_engine.api.deps import (
    CurrentUser,
    ReauthGrant,
    provider_bindings,
    reaches_org,
    reads_every_org,
    refuse_unknown_orgs,
    refuse_unreachable_org,
    refuse_without_grant,
    require_action,
    require_own_org_action,
    require_provider_reauth,
    secrets_store,
)
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.identity_links import unlink_provider
from dfe_engine.auth.oidc.credential_env import (
    env_var_resolves,
    is_env_var_name,
    provider_secret_path,
)
from dfe_engine.auth.oidc.field_rules import FieldProblem, field_problems
from dfe_engine.auth.oidc.models import SyncStatus, checked_email_domains
from dfe_engine.auth.oidc.sync import SyncOutcome
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.auth.reauth import grant_target
from dfe_engine.auth.scim_mapping import SCIM_SOURCE_PROVIDER
from dfe_engine.governance.ch import request_ch_rbac_reconcile
from dfe_engine.http_errors import (
    describe_http_error,
    failure_reference,
    http_failure_message,
    referenced,
)
from dfe_engine.names import StoreName
from dfe_engine.orgs.role_groups import usable_org_admins

if TYPE_CHECKING:
    from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider
    from dfe_engine.auth.oidc.registry import OIDCProviderRegistry


router = APIRouter(prefix="/oidc-providers", tags=["OIDC Providers"])

_ENV_NAME_HELP = (
    "must be an environment variable name such as OKTA_CLIENT_SECRET, not the "
    "credential itself - send the credential in the matching value field instead"
)

# How a secret's *_env field moves it between the secret store and an env var.
_ENV_SOURCE_HELP = (
    "Omit it on an update to keep the name and any stored copy or send an empty string to "
    "clear the name and keep the stored copy. Naming an env var with no value for the secret "
    "moves the secret onto it and the stored copy is deleted once the update is saved; "
    "resending the name already held, with nothing stored, changes nothing. When that move, "
    "or a create naming an env var with no value for the secret, names one the engine does "
    "not set, the request is refused with 422 env_var_not_set on this field and nothing is "
    "saved or deleted."
)

# An Entra tenant GUID or domain: letters, digits, dots and hyphens, starting with a letter or digit.
_TENANT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z")

# An RFC 6749 section 3.3 scope-token: printable ASCII except space, double quote and backslash.
_SCOPE_TOKEN = re.compile(r"[\x21\x23-\x5B\x5D-\x7E]+\Z")

_ORG_HELP = (
    "The org whose provider this is: a registered org's name, or empty for a platform "
    "provider. Its sign-ins land in that org as viewers and link only to that org's "
    "accounts. An admin of that org manages it."
)

_TRUSTED_EMAIL_DOMAINS_HELP = (
    "Email domains this provider vouches for, such as corp.com. A first sign-in whose "
    "verified email is in one of them links to the one account holding that email: "
    "at once for an account an identity provider owns, after the account's password "
    "for a local one. Stored lowercased. Empty links nothing."
)

_SCOPES_HELP = (
    "OAuth scopes requested at login, one scope per entry; must include openid. One "
    "space-separated string is accepted too, and a blank one is no override. Omit for "
    "the provider type's default: openid email profile, plus groups for generic and "
    "okta, and https://www.googleapis.com/auth/cloud-identity.groups.readonly for "
    "google, which reads each user's groups with their own token. Google and Entra ID "
    "fail a login that asks for groups."
)

# Each directory secret a groups block can carry: its request field, its secret-store field and the config field that keeps the path.
_GROUP_SECRETS = (
    ("api_token", "groups_api_token", "api_token_path"),
    ("client_secret", "groups_client_secret", "client_secret_path"),
    ("service_account_json", "groups_service_account_json", "service_account_json_path"),
)

# The groups block's env var names: omitted keeps the stored name, an empty string clears it.
_GROUP_ENV_FIELDS = (
    "api_token_env",
    "client_secret_env",
    "service_account_json_env",
    "tenant_id_env",
)


def _reject_non_env_name(value: str) -> str:
    """Refuse a credential pasted where an env var NAME belongs."""
    if value and not is_env_var_name(value):
        raise ValueError(_ENV_NAME_HELP)
    return value


def _scopes_from_string(value: object) -> object:
    """A space-separated scope string as its list of scopes, or None when blank. Anything else unchanged.

    The provider model and the setup status carry scopes as one string, so a form
    filled from either sends that shape back.
    """
    if isinstance(value, str):
        return value.split() or None
    return value


def _checked_scopes(scopes: list[str]) -> list[str]:
    """The scopes with duplicates dropped, refused unless each is one scope token and ``openid`` is among them."""
    if not all(_SCOPE_TOKEN.match(scope) for scope in scopes):
        raise ValueError(
            "each entry must be a single scope, with no spaces, double quotes or backslashes"
        )
    if "openid" not in scopes:
        raise ValueError("must include openid: without it the provider issues no ID token")
    return list(dict.fromkeys(scopes))


# -- Request / Response models --------------------------------


class GroupResolutionRequest(BaseModel):
    """How a provider's groups resolve: by hand, from a token claim, or from its API."""

    mode: Literal["manual", "token_claim", "api"] = Field(
        default="token_claim",
        description="Group resolution mode: manual, token_claim, api",
    )
    claim_name: str = Field(default="groups", description="Token claim name for group IDs")
    sync_interval: int = Field(default=3600, ge=60, description="Seconds between API sync cycles")
    enrich_on_login: bool = Field(
        default=False,
        description="Fetch the user's groups from the directory API at each login "
        "(needed by providers with no groups claim, e.g. Google Workspace)",
    )
    service_account_json: str = Field(
        default="",
        description="Optional Google service account JSON, for the group sync and for "
        "logins whose own token cannot read their groups. It acts as itself, so the "
        "org's admin must assign it a groups admin role. Write-only: it goes to the "
        "secret store and only its path is kept in config.",
    )
    service_account_json_env: str | None = Field(
        default=None,
        description=f"Env var for the Google service account JSON. {_ENV_SOURCE_HELP}",
    )
    domain: str = Field(default="", description="Google Workspace domain the service account syncs")
    tenant_id: str = Field(
        default="", description="Entra ID tenant ID. Not a secret, so it is stored in config."
    )
    tenant_id_env: str | None = Field(
        default=None,
        description="Env var for Entra ID tenant ID, read when tenant_id is empty. Omit it on "
        "an update to keep the name held; send an empty string to clear it.",
    )
    client_secret: str = Field(
        default="",
        description="Entra ID application client secret. Write-only: it goes to the "
        "secret store and only its path is kept in config. When neither this nor "
        "client_secret_env is set, the login client secret is used.",
    )
    client_secret_env: str | None = Field(
        default=None, description=f"Env var for the Entra ID client secret. {_ENV_SOURCE_HELP}"
    )
    api_token: str = Field(
        default="",
        description="Okta API token. Write-only: it goes to the secret store and only "
        "its path is kept in config.",
    )
    api_token_env: str | None = Field(
        default=None, description=f"Env var for the Okta API token. {_ENV_SOURCE_HELP}"
    )
    okta_domain: str = Field(default="", description="Okta organisation domain")

    @field_validator(*_GROUP_ENV_FIELDS)
    @classmethod
    def _env_name_only(cls, value: str | None) -> str | None:
        return value if value is None else _reject_non_env_name(value)

    @field_validator("tenant_id")
    @classmethod
    def _tenant_id_shape(cls, value: str) -> str:
        """The tenant id becomes a path segment of the Entra authority URL, so it may only be a GUID or a domain."""
        if value and not (_TENANT_ID.match(value)):
            raise ValueError(
                "must be the tenant's GUID or its domain, such as contoso.onmicrosoft.com"
            )
        return value


class CreateProviderRequest(BaseModel):
    # A filename stem and a secret-store path segment, refused before any secret is written.
    """Body for registering an OIDC provider."""

    name: StoreName = Field(description="Unique provider name (used as filename stem)")
    type: Literal["generic", "google", "entra_id", "okta"] = Field(
        description="Provider type: generic, google, entra_id, okta"
    )
    org: str = Field(default="", description=_ORG_HELP)
    enabled: bool = Field(default=True, description="Whether the provider serves logins")
    display_name: str = Field(default="", description="Human-readable label")
    issuer: str = Field(default="", description="OIDC issuer URL")
    client_id: str = Field(
        default="", description="OIDC client ID. Not a secret, so it is stored in config."
    )
    client_id_env: str = Field(default="", description="Env var name for OIDC client ID")
    client_secret: str = Field(
        default="",
        description="RP client secret used in the auth-code exchange. Write-only: it "
        "goes to the secret store and only its path is kept in config.",
    )
    client_secret_env: str = Field(
        default="",
        description="Env var name for the RP client secret used in the auth-code exchange. "
        "With no client_secret, one the engine does not set is refused with 422 "
        "env_var_not_set on this field and nothing is saved.",
    )
    scopes: list[str] | None = Field(default=None, description=_SCOPES_HELP)
    groups: GroupResolutionRequest = Field(default_factory=GroupResolutionRequest)
    trusted_email_domains: list[str] = Field(
        default_factory=list, description=_TRUSTED_EMAIL_DOMAINS_HELP
    )

    @field_validator("client_id_env", "client_secret_env")
    @classmethod
    def _env_name_only(cls, value: str) -> str:
        return _reject_non_env_name(value)

    @field_validator("trusted_email_domains")
    @classmethod
    def _bare_domains(cls, value: list[str]) -> list[str]:
        return checked_email_domains(domains=value)

    @field_validator("scopes", mode="before")
    @classmethod
    def _scopes_as_list(cls, value: object) -> object:
        return _scopes_from_string(value)

    @field_validator("scopes")
    @classmethod
    def _scopes_shape(cls, value: list[str] | None) -> list[str] | None:
        return value if value is None else _checked_scopes(value)


class UpdateProviderRequest(BaseModel):
    """Body for updating a provider, where an omitted field keeps its value."""

    org: str | None = Field(None, description=f"Move the provider to another org. {_ORG_HELP}")
    enabled: bool | None = Field(None, description="Enable or disable the provider")
    display_name: str | None = Field(None, description="Human-readable label")
    issuer: str | None = Field(
        None, description="OIDC issuer URL. An empty one is refused, since a login needs it"
    )
    client_id: str | None = Field(None, description="OIDC client ID (not a secret)")
    client_id_env: str | None = Field(None, description="Env var name for OIDC client ID")
    client_secret: str | None = Field(
        None,
        description="Rotate the RP client secret. Write-only: it goes to the secret "
        "store and only its path is kept in config.",
    )
    client_secret_env: str | None = Field(
        None, description=f"Env var name for the RP client secret. {_ENV_SOURCE_HELP}"
    )
    scopes: list[str] | None = Field(
        None,
        description="Replace the OAuth scopes requested at login, one scope per entry; "
        "must include openid. One space-separated string is accepted too, and a blank "
        "one leaves the scopes as they are.",
    )
    groups: GroupResolutionRequest | None = Field(None, description="Group resolution config")
    trusted_email_domains: list[str] | None = Field(
        None, description=f"Replace the trusted email domains. {_TRUSTED_EMAIL_DOMAINS_HELP}"
    )

    @field_validator("client_id_env", "client_secret_env")
    @classmethod
    def _env_name_only(cls, value: str | None) -> str | None:
        return value if value is None else _reject_non_env_name(value)

    @field_validator("trusted_email_domains")
    @classmethod
    def _bare_domains(cls, value: list[str] | None) -> list[str] | None:
        return value if value is None else checked_email_domains(domains=value)

    @field_validator("scopes", mode="before")
    @classmethod
    def _scopes_as_list(cls, value: object) -> object:
        return _scopes_from_string(value)

    @field_validator("scopes")
    @classmethod
    def _scopes_shape(cls, value: list[str] | None) -> list[str] | None:
        return value if value is None else _checked_scopes(value)


class GroupResolutionResponse(BaseModel):
    """A provider's group resolution, with secret paths in place of values."""

    mode: str
    claim_name: str
    sync_interval: int
    enrich_on_login: bool
    service_account_json_env: str
    service_account_json_path: str
    domain: str
    tenant_id: str
    tenant_id_env: str
    client_secret_env: str
    client_secret_path: str
    api_token_env: str
    api_token_path: str
    okta_domain: str


class ProviderResponse(BaseModel):
    """Provider config -- env var names and secret paths, never a secret value."""

    name: str
    type: str
    org: str = Field(description=_ORG_HELP)
    enabled: bool
    display_name: str
    issuer: str
    client_id: str
    client_id_env: str
    client_secret_env: str
    client_secret_path: str
    scopes: list[str] = Field(description="OAuth scopes requested at login")
    groups: GroupResolutionResponse
    trusted_email_domains: list[str] = Field(description=_TRUSTED_EMAIL_DOMAINS_HELP)
    created_at: str
    last_sync_at: str
    last_sync_status: SyncStatus = Field(
        description="How the last group sync ended: empty before the first, ok, error "
        "(sync_error says why), not_configured (no directory credential), user_token (each "
        "login reads its own groups, so there is no directory to sync), or 'partial: ' "
        "followed by how many groups were skipped and why.",
    )
    sync_error: str


class OrphanedGroupInfo(BaseModel):
    """A group its provider no longer backs."""

    name: str
    source_provider: str
    roles: list[str]
    member_count: int


class DisabledAccountInfo(BaseModel):
    """An account soft-disabled and anonymized because its OIDC provider was detached."""

    username: str = Field(description="Anonymized account id (``{uuid}-deleted``)")
    previous_username: str = Field(description="Account id before anonymization")
    source_provider: str = Field(
        description="Ownership stamp on the account (OIDC provider name, scim, or empty)"
    )
    oidc_id: str = Field(description="OIDC provider name last used to log this account in")


class DetachResponse(BaseModel):
    """What detaching a provider deleted, orphaned and disabled."""

    deleted: str
    orphaned_groups: list[OrphanedGroupInfo]
    disabled_accounts: list[DisabledAccountInfo] = Field(
        default_factory=list,
        description=(
            "Accounts soft-disabled and renamed to ``{uuid}-deleted`` because they were "
            "owned by or last logged in through the detached provider. Already-disabled "
            "and protected recovery accounts are left alone."
        ),
    )


class SyncResponse(BaseModel):
    """The outcome of a group sync."""

    created: int
    updated: int
    total: int
    groups_skipped: int = Field(
        default=0,
        description="Provider groups left unsynced: an identifier that makes no valid group "
        "name, a name held by a stored group that does not load, a name held by a stored "
        "group not linked to that provider group, a name another group carries as its "
        "source_id or an id another group has as its name, a stored group carrying the id but linked "
        "to another provider, no id to link a group by, or a group the store could not "
        "write. Each is counted on auth_oidc_sync_groups_skipped_total{reason}.",
    )
    error: str | None
    skipped: str | None = Field(
        default=None,
        description="Why the sync did nothing, when it did not run: the provider is disabled, "
        "is not in api mode, has no directory credential configured, or reads each login's "
        "groups with the user's own token and has no directory to sync. The provider's "
        "last_sync_status reads not_configured and user_token in those last two cases.",
    )
    status: SyncOutcome = Field(
        description="How the sync ended: ok, partial (some groups skipped), error (the "
        "listing failed, the secret store holding its credential did not answer or "
        "refused access, or the group store failed part way), "
        "not_configured (no directory credential), user_token (no directory to sync, "
        "which is a Google provider's default and not a fault) or skipped (disabled or "
        "not in api mode). Key on this, never on the text of error or skipped.",
    )


class TestResponse(BaseModel):
    """Whether the provider answered the connection test."""

    success: bool
    message: str


class LoginConfigCheck(BaseModel):
    """One check in the login-config verification."""

    name: str
    ok: bool
    detail: str


class LoginConfigResponse(BaseModel):
    """Result of verifying a provider's OIDC LOGIN configuration.

    Distinct from ``/{name}/test``, which verifies the group-DIRECTORY
    credentials (Graph / Okta / Google) and is a no-op for a generic provider.
    This verifies the login half: discovery reachable, and the RP client
    credentials present - the things that make ``/auth/oidc/{name}/login``
    actually work.
    """

    ok: bool
    checks: list[LoginConfigCheck]


# -- Helpers --------------------------------------------------


_GROUP_ENV_FIELDS = (
    "api_token_env",
    "client_secret_env",
    "service_account_json_env",
    "tenant_id_env",
)


def _env_sources(
    *,
    client_id_env: str | None,
    client_secret_env: str | None,
    groups: GroupResolutionRequest | GroupResolutionConfig | None,
) -> dict[str, str]:
    """Every env var name a provider body or stored provider names, by field."""
    named = {"client_id_env": client_id_env or "", "client_secret_env": client_secret_env or ""}
    if groups is not None:
        for field in _GROUP_ENV_FIELDS:
            named[f"groups.{field}"] = getattr(groups, field) or ""
    return named


def _refuse_env_sources(
    *, named: dict[str, str], request: Request, stored: dict[str, str], user: Any
) -> None:
    """Refuse 422 ``env_source_refused`` when a caller who does not act across every org names an env var the provider does not already hold, since the engine would read its own environment for it."""
    if reads_every_org(request, user, scopes_dict["oidc_write"]):
        return
    for field, env in sorted(named.items()):
        if (env) and (env != stored.get(field, "")):
            raise ProblemError(
                422,
                f"{field} names env var {env!r}, which only an admin of every org may set: "
                "send the credential's value instead",
                code="env_source_refused",
            )


def _refuse_repointed_env_sources(
    *, body: UpdateProviderRequest, current: OIDCProvider, request: Request, user: Any
) -> None:
    """Refuse 422 ``env_source_held`` when a caller who does not act across every org moves where a provider sends credentials while it still reads one from an env var an admin of every org set."""
    if reads_every_org(request, user, scopes_dict["oidc_write"]):
        return
    stored = _env_sources(
        client_id_env=current.client_id_env,
        client_secret_env=current.client_secret_env,
        groups=current.groups,
    )
    # None is a field the body leaves out, which keeps its stored env var. Empty clears it.
    sent = {"client_id_env": body.client_id_env, "client_secret_env": body.client_secret_env}
    if body.groups is not None:
        sent.update((f"groups.{field}", getattr(body.groups, field)) for field in _GROUP_ENV_FIELDS)
    held = [field for field, env in stored.items() if (env) and (sent.get(field) != "")]
    moved = (body.issuer is not None) and (body.issuer != current.issuer)
    if body.groups is not None:
        moved = (moved) or any(
            getattr(body.groups, field) != getattr(current.groups, field)
            for field in ("domain", "okta_domain", "tenant_id")
        )
    if (held) and (moved):
        raise ProblemError(
            422,
            f"The provider reads {held[0]} from an env var an admin of every org set, so its "
            "issuer and directory may not change: clear the env var sources first",
            code="env_source_held",
        )


def _refuse_stranded_sign_ins(*, name: str, org: str, request: Request) -> None:
    """Refuse 422 ``provider_in_use`` when moving provider *name* to *org* would leave accounts it signs in unreachable, since strict pairing would refuse their next sign-in."""
    home = (request.app.state.org_registry.home_name_known()) or ""
    reached = {org} if org else {"", home}
    for account in request.app.state.account_store.scan():
        stamped = name in {
            account.oidc_id,
            account.source_provider,
            *(identity.provider for identity in account.identities),
        }
        if (stamped) and (account.org not in reached):
            raise ProblemError(
                422,
                f"Provider {name!r} signs in accounts the move would leave unreachable: "
                "move or remove them first",
                code="provider_in_use",
            )


def _refuse_reserved_name(*, name: str, org: str, request: Request) -> None:
    """Refuse 422 ``provider_name_reserved`` for an org's provider named after a stamp sign-ins arrive under from outside that org."""
    if not (org):
        return
    bindings = provider_bindings(request)
    reserved = {
        request.app.state.settings.auth.proxy_provider,
        SCIM_SOURCE_PROVIDER,
        *bindings,
        *bindings.values(),
    }
    stamped = any(
        (account.org != org)
        and (
            name
            in {
                account.oidc_id,
                account.source_provider,
                *(identity.provider for identity in account.identities),
            }
        )
        for account in request.app.state.account_store.scan()
    )
    linked = any(group.source_provider == name for group in request.app.state.group_store.list())
    if (name in reserved) or (stamped) or (linked):
        raise ProblemError(
            422,
            f"Provider name {name!r} is reserved: sign-ins already arrive under it from "
            f"outside org {org!r}",
            code="provider_name_reserved",
        )


def _refuse_domain_of_another_org(
    *, domains: list[str], name: str, org: str, registry: OIDCProviderRegistry
) -> None:
    """Refuse 422 ``domain_taken`` when a provider of another org already trusts one of *domains*, since a domain belongs to one org."""
    wanted = set(domains)
    for other_name, other in registry.list():
        clash = sorted(wanted & set(other.trusted_email_domains))
        if (other_name != name) and (other.org != org) and (clash):
            raise ProblemError(
                422,
                f"Email domain {clash[0]!r} is trusted by another organisation's provider, "
                "and a domain belongs to one organisation",
                code="domain_taken",
            )


def _require_reachable_provider(
    *, action: str, name: str, request: Request, user: Any
) -> OIDCProvider:
    """The provider *name*, 404 when it is missing or the caller may not take *action* on its org, so another org's providers stay hidden."""
    provider = _get_registry(request).get(name)
    reachable = (provider is not None) and (
        reaches_org(action=action, org=provider.org, request=request, user=user)
    )
    if (provider is None) or (not (reachable)):
        raise ProblemError(404, f"OIDC provider '{name}' not found", code="not_found")
    return provider


def _provider_to_response(name: str, provider: OIDCProvider) -> ProviderResponse:
    """Convert an OIDCProvider model to a ProviderResponse."""
    p = provider
    return ProviderResponse(
        name=name,
        type=p.type,
        org=p.org,
        enabled=p.enabled,
        display_name=p.display_name,
        issuer=p.issuer,
        client_id=p.client_id,
        client_id_env=p.client_id_env,
        client_secret_env=p.client_secret_env,
        client_secret_path=p.client_secret_path,
        scopes=p.scopes.split(),
        groups=GroupResolutionResponse(
            mode=p.groups.mode,
            claim_name=p.groups.claim_name,
            sync_interval=p.groups.sync_interval,
            enrich_on_login=p.groups.enrich_on_login,
            service_account_json_env=p.groups.service_account_json_env,
            service_account_json_path=p.groups.service_account_json_path,
            domain=p.groups.domain,
            tenant_id=p.groups.tenant_id,
            tenant_id_env=p.groups.tenant_id_env,
            client_secret_env=p.groups.client_secret_env,
            client_secret_path=p.groups.client_secret_path,
            api_token_env=p.groups.api_token_env,
            api_token_path=p.groups.api_token_path,
            okta_domain=p.groups.okta_domain,
        ),
        trusted_email_domains=p.trusted_email_domains,
        created_at=p.created_at,
        last_sync_at=p.last_sync_at,
        last_sync_status=p.last_sync_status,
        sync_error=p.sync_error,
    )


def _get_registry(request: Request) -> OIDCProviderRegistry:
    """Return OIDCProviderRegistry from app state."""
    registry: OIDCProviderRegistry = request.app.state.oidc_provider_registry
    return registry


def _store_secret(*, field: str, name: str, request: Request, value: str) -> None:
    """Write one provider secret through the secrets interface, at the path the provider records."""
    secrets_store(request).put(provider_secret_path(field=field, provider_name=name), value)


def _group_config(
    *, groups: GroupResolutionRequest, kept: Mapping[str, object], provider_type: str
) -> GroupResolutionConfig:
    """The groups block to store for a request: its fields minus the secret values, plus what the provider keeps.

    A google provider always enriches on login, because its tokens carry no groups.
    """
    from dfe_engine.auth.oidc.models import GroupResolutionConfig

    secret_values = {value_field for value_field, _store_field, _path_field in _GROUP_SECRETS}
    # An env var name left out is None: *kept* carries the stored one on an update, the default on a create.
    fields = {**groups.model_dump(exclude=secret_values, exclude_none=True), **kept}
    if provider_type == "google":
        fields["enrich_on_login"] = True
    return GroupResolutionConfig.model_validate(fields)


def _group_secret_paths(*, groups: GroupResolutionRequest, name: str) -> dict[str, str]:
    """The store path of each directory secret the groups block carries, keyed by the config field that keeps it."""
    return {
        path_field: provider_secret_path(field=store_field, provider_name=name)
        for value_field, store_field, path_field in _GROUP_SECRETS
        if getattr(groups, value_field)
    }


def _refuse_field_problems(*, problems: list[FieldProblem]) -> None:
    """Answer 422 naming each field the provider rules refuse, the same way a malformed body is answered."""
    if problems:
        raise RequestValidationError(
            [
                {
                    "loc": ("body", *problem.field.split(".")),
                    "msg": problem.message,
                    "type": problem.code,
                }
                for problem in problems
            ]
        )


def _sent_group_fields(*, groups: GroupResolutionRequest | None) -> frozenset[str]:
    """The groups fields the caller set to something, so the rules refuse only what this request sends."""
    if groups is None:
        return frozenset()
    return frozenset(field for field, value in groups.model_dump().items() if value)


def _store_group_secrets(*, groups: GroupResolutionRequest, name: str, request: Request) -> None:
    """Write whichever directory-API secrets the groups block carries. A secret it omits keeps the one already stored."""
    for value_field, store_field, _path_field in _GROUP_SECRETS:
        if getattr(groups, value_field):
            _store_secret(
                field=store_field, name=name, request=request, value=getattr(groups, value_field)
            )


def _delete_secrets(*, paths: Iterable[str], request: Request) -> None:
    """Remove the secret at each non-empty store path. With no secrets backend nothing was stored.

    It runs once the provider is saved, which no longer reads these paths, so a delete the store fails is logged by its path and left for an operator. The saved change still answers.
    """
    store = getattr(request.app.state, "dfe_secrets", None)
    if store is None:
        return
    for path in paths:
        if not (path):
            continue
        try:
            store.delete(path)
        except Exception as exc:
            logger.warning(
                "OIDC provider secret left in the secret store: the provider no longer reads it",
                store_path=path,
                **describe_http_error(exc),
            )


def _delete_stored_secrets(request: Request, provider: OIDCProvider) -> None:
    """Remove every secret the provider recorded a path for."""
    group_paths = [
        getattr(provider.groups, path_field)
        for _value_field, _store_field, path_field in _GROUP_SECRETS
    ]
    _delete_secrets(paths=(provider.client_secret_path, *group_paths), request=request)


def _env_var_not_set(*, env: str, field: str) -> FieldProblem:
    """The problem answered on *field* when a secret would move onto env var *env* and the engine does not set it."""
    return FieldProblem(
        code="env_var_not_set",
        field=field,
        message=f"the env var {env!r} is not set in the engine, so the secret would resolve "
        "empty: set it in the engine's environment first or send the secret's value",
    )


def _group_env_moves(
    *, current: GroupResolutionConfig | None, groups: GroupResolutionRequest
) -> tuple[list[str], list[FieldProblem]]:
    """The config field keeping the path of each directory secret the groups block moves onto its env var (:func:`_moves_to_env`) plus a problem for each move onto an env var the engine does not set.

    *current* is the stored groups block, None on a create.
    """
    moved = []
    problems = []
    for value_field, _store_field, path_field in _GROUP_SECRETS:
        env_field = f"{value_field}_env"
        env = getattr(groups, env_field)
        if env is None:
            continue
        moves = _moves_to_env(
            env=env,
            stored_env=getattr(current, env_field) if current is not None else "",
            stored_path=getattr(current, path_field) if current is not None else "",
            value=getattr(groups, value_field),
        )
        if not (moves):
            continue
        if env_var_resolves(env_name=env):
            moved.append(path_field)
        else:
            problems.append(_env_var_not_set(env=env, field=f"groups.{env_field}"))
    return moved, problems


def _moves_to_env(*, env: str, stored_env: str, stored_path: str, value: str) -> bool:
    """Whether a write naming env var *env* for a secret and sending *value* for it moves the secret onto that env var.

    It does when no value is sent and either the name is new or a stored copy would still be read before the env var. Resending the name already held with nothing stored changes nothing.
    """
    if (value) or not (env):
        return False
    return (env != stored_env) or (bool(stored_path))


async def _refresh_rp(request: Request) -> None:
    """Rebuild the relying party so this write takes effect on the login routes.

    The registry is read fresh from YAML on every call, but the RP snapshots the
    enabled providers and their resolved credentials when it is built. Without
    this rebuild a provider created here 404s on ``/auth/oidc/{name}/login``
    until the process restarts. Non-raising - see ``build_relying_party``. The
    rebuild reads each credential from the secret store, so it runs on a worker thread.
    """
    from dfe_engine.auth.oidc.rp import build_relying_party

    request.app.state.oidc_rp = await run_blocking(
        functools.partial(
            build_relying_party,
            _get_registry(request),
            secrets=getattr(request.app.state, "dfe_secrets", None),
            metrics=getattr(request.app.state, "oidc_login_metrics", None),
            directory_deadline_seconds=(
                request.app.state.settings.auth.oidc_login_directory_deadline_seconds
            ),
            crypto_profile=request.app.state.settings.crypto_profile,
        )
    )


# -- Endpoints ------------------------------------------------


def _refuse_new_provider(*, body: CreateProviderRequest, request: Request, user) -> OIDCProvider:
    """Raise every refusal a provider create answers before it writes, and return the provider it would store."""
    from dfe_engine.auth.oidc.models import OIDCProvider

    refuse_unreachable_org(
        action=scopes_dict["oidc_write"],
        noun="OIDC providers",
        org=body.org,
        request=request,
        user=user,
    )
    refuse_unknown_orgs(request, [body.org])
    registry = _get_registry(request)
    if registry.get(body.name) is not None:
        raise ProblemError(409, f"OIDC provider '{body.name}' already exists", code="conflict")
    _refuse_domain_of_another_org(
        domains=body.trusted_email_domains, name=body.name, org=body.org, registry=registry
    )
    _refuse_reserved_name(name=body.name, org=body.org, request=request)
    _refuse_env_sources(
        named=_env_sources(
            client_id_env=body.client_id_env,
            client_secret_env=body.client_secret_env,
            groups=body.groups,
        ),
        request=request,
        stored={},
        user=user,
    )

    client_secret_path = (
        provider_secret_path(field="client_secret", provider_name=body.name)
        if body.client_secret
        else ""
    )
    group_paths = _group_secret_paths(groups=body.groups, name=body.name)
    fields: dict[str, Any] = {
        "type": body.type,
        "org": body.org,
        "enabled": body.enabled,
        "display_name": body.display_name,
        "issuer": body.issuer,
        "client_id": body.client_id,
        "client_id_env": body.client_id_env,
        "client_secret_env": body.client_secret_env,
        "client_secret_path": client_secret_path,
        # Empty takes the provider type's default scopes.
        "scopes": " ".join(body.scopes) if body.scopes is not None else "",
        "groups": _group_config(groups=body.groups, kept=group_paths, provider_type=body.type),
        "trusted_email_domains": body.trusted_email_domains,
        "created_at": datetime.now(UTC).isoformat(),
    }
    # The rules run before the model's validators, so a pair both refuse answers 422 on its field.
    rule_problems = field_problems(
        provider=OIDCProvider.model_construct(**fields),
        sent_group_fields=_sent_group_fields(groups=body.groups),
    )
    _moved, env_problems = _group_env_moves(current=None, groups=body.groups)
    if _moves_to_env(
        env=body.client_secret_env, stored_env="", stored_path="", value=body.client_secret
    ) and not (env_var_resolves(env_name=body.client_secret_env)):
        env_problems.insert(
            0, _env_var_not_set(env=body.client_secret_env, field="client_secret_env")
        )
    _refuse_field_problems(problems=[*rule_problems, *env_problems])
    provider = OIDCProvider(**fields)
    return provider


async def refuse_new_provider(
    body: CreateProviderRequest, request: Request, user: CurrentUser
) -> None:
    """FastAPI dependency: answer a create's own refusals before its re-auth grant is asked for."""
    _refuse_new_provider(body=body, request=request, user=user)


async def require_new_provider_reauth(
    body: CreateProviderRequest, request: Request, user: CurrentUser, reauth: ReauthGrant = ""
) -> None:
    """FastAPI dependency: the caller's re-auth grant for the sign-in provider the body creates."""
    target = grant_target(kind="provider", name=body.name)
    refuse_without_grant(reauth=reauth, request=request, target=target, user=user)


@router.post(
    "",
    response_model=ProviderResponse,
    status_code=201,
    dependencies=[
        Depends(require_own_org_action(scopes_dict["oidc_write"])),
        Depends(refuse_new_provider),
        Depends(require_new_provider_reauth),
    ],
)
async def create_provider(
    body: CreateProviderRequest,
    user: CurrentUser,
    request: Request,
):
    """Create a new OIDC provider configuration (admin only).

    Secret values in the body are written to the secret store before the YAML, and only once the name is free and the provider passes the field rules for its type and mode, so a refused provider leaves nothing behind. A secret named by an env var the engine does not set, with no value sent for it, is refused the same way (``env_var_not_set``).
    """
    registry = _get_registry(request)
    provider = _refuse_new_provider(body=body, request=request, user=user)

    await run_blocking(
        lambda: _store_group_secrets(groups=body.groups, name=body.name, request=request)
    )
    if body.client_secret:
        secret = body.client_secret
        await run_blocking(
            lambda: _store_secret(
                field="client_secret", name=body.name, request=request, value=secret
            )
        )

    try:
        registry.create(body.name, provider)
    except ValueError:
        raise ProblemError(409, f"OIDC provider '{body.name}' already exists", code="conflict")

    await _refresh_rp(request)
    audit_resource_change(user.user_id, "oidc_provider", body.name, "created")
    return _provider_to_response(body.name, provider)


ProvidersList = Annotated[
    ListQuery, Depends(list_query(order_by=("name", "type", "created_at"), tiebreak="name"))
]


@router.get(
    "",
    response_model=Page[ProviderResponse],
    dependencies=[Depends(require_own_org_action(scopes_dict["oidc_read"]))],
)
async def list_providers(
    user: CurrentUser,
    request: Request,
    query: ProvidersList,
    search: str | None = Query(None, description="Search in provider name/display name"),
):
    """List the OIDC providers the caller manages, paginated: every one across orgs, else its orgs' own."""
    registry = _get_registry(request)
    action = scopes_dict["oidc_read"]
    rows = [
        _provider_to_response(name, p).model_dump()
        for name, p in registry.list()
        if reaches_org(action=action, org=p.org, request=request, user=user)
    ]
    rows = apply_search(rows, search, ["name", "display_name"])
    rows = sort_by_order(rows, query.order_by)
    summaries = [ProviderResponse.model_validate(row) for row in rows]
    return paginate(summaries, query, bound={"search": search})


@router.get(
    "/{name}",
    response_model=ProviderResponse,
    dependencies=[Depends(require_own_org_action(scopes_dict["oidc_read"]))],
)
async def get_provider(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Get a single OIDC provider by name, 404 for one of an org the caller does not manage."""
    provider = _require_reachable_provider(
        action=scopes_dict["oidc_read"], name=name, request=request, user=user
    )
    return _provider_to_response(name, provider)


class _ProviderUpdate(NamedTuple):
    """A provider update that passed its refusals: the stored provider, the fields to write and the secret paths to drop once it is saved."""

    current: OIDCProvider
    stale_paths: list[str]
    update_fields: dict[str, object]


def _refuse_provider_update(
    *, body: UpdateProviderRequest, name: str, request: Request, user
) -> _ProviderUpdate:
    """Raise every refusal a provider update answers before it writes a secret or the config, and return what it writes."""
    action = scopes_dict["oidc_write"]
    registry = _get_registry(request)
    current = _require_reachable_provider(action=action, name=name, request=request, user=user)
    org = current.org if body.org is None else body.org
    if org != current.org:
        refuse_unreachable_org(
            action=action, noun="OIDC providers", org=org, request=request, user=user
        )
        refuse_unknown_orgs(request, [org])
        _refuse_reserved_name(name=name, org=org, request=request)
        _refuse_stranded_sign_ins(name=name, org=org, request=request)
    _refuse_env_sources(
        named=_env_sources(
            client_id_env=body.client_id_env,
            client_secret_env=body.client_secret_env,
            groups=body.groups,
        ),
        request=request,
        stored=_env_sources(
            client_id_env=current.client_id_env,
            client_secret_env=current.client_secret_env,
            groups=current.groups,
        ),
        user=user,
    )
    _refuse_repointed_env_sources(body=body, current=current, request=request, user=user)
    if (body.trusted_email_domains is not None) or (org != current.org):
        domains = (
            current.trusted_email_domains
            if body.trusted_email_domains is None
            else body.trusted_email_domains
        )
        _refuse_domain_of_another_org(domains=domains, name=name, org=org, registry=registry)

    stale_paths = []
    env_problems = []
    update_fields: dict[str, object] = {}
    if body.org is not None:
        update_fields["org"] = body.org
    if body.enabled is not None:
        update_fields["enabled"] = body.enabled
    if body.display_name is not None:
        update_fields["display_name"] = body.display_name
    if body.issuer is not None:
        update_fields["issuer"] = body.issuer
    if body.client_id is not None:
        update_fields["client_id"] = body.client_id
    if body.client_id_env is not None:
        update_fields["client_id_env"] = body.client_id_env
    if body.client_secret_env is not None:
        update_fields["client_secret_env"] = body.client_secret_env
    if body.scopes is not None:
        update_fields["scopes"] = " ".join(body.scopes)
    if body.trusted_email_domains is not None:
        update_fields["trusted_email_domains"] = body.trusted_email_domains
    if body.client_secret:
        update_fields["client_secret_path"] = provider_secret_path(
            field="client_secret", provider_name=name
        )
    elif body.client_secret_env is not None and _moves_to_env(
        env=body.client_secret_env,
        stored_env=current.client_secret_env,
        stored_path=current.client_secret_path,
        value="",
    ):
        if env_var_resolves(env_name=body.client_secret_env):
            update_fields["client_secret_path"] = ""
            stale_paths.append(current.client_secret_path)
        else:
            env_problems.append(
                _env_var_not_set(env=body.client_secret_env, field="client_secret_env")
            )
    if body.groups is not None:
        kept = {
            path_field: getattr(current.groups, path_field)
            for _value_field, _store_field, path_field in _GROUP_SECRETS
        }
        # The directory backend is not settable through this API, so a groups edit carries it over.
        kept.update(
            directory_backend=current.groups.directory_backend,
            mock_directory_env=current.groups.mock_directory_env,
        )
        kept.update(
            (env_field, getattr(current.groups, env_field))
            for env_field in _GROUP_ENV_FIELDS
            if getattr(body.groups, env_field) is None
        )
        moved_to_env, group_env_problems = _group_env_moves(
            current=current.groups, groups=body.groups
        )
        env_problems.extend(group_env_problems)
        stale_paths.extend(getattr(current.groups, path_field) for path_field in moved_to_env)
        kept.update(dict.fromkeys(moved_to_env, ""))
        kept.update(_group_secret_paths(groups=body.groups, name=name))
        update_fields["groups"] = _group_config(
            groups=body.groups, kept=kept, provider_type=current.type
        )

    rule_problems = []
    if body.model_dump(
        exclude={"display_name", "enabled", "org", "trusted_email_domains"}, exclude_none=True
    ):
        candidate = current.model_copy(update=update_fields)
        problems = field_problems(
            provider=candidate, sent_group_fields=_sent_group_fields(groups=body.groups)
        )
        # A stored provider with no issuer is not the caller's to fix unless the update sets one.
        rule_problems = [
            problem for problem in problems if problem.field != "issuer" or body.issuer is not None
        ]
    _refuse_field_problems(problems=[*rule_problems, *env_problems])
    return _ProviderUpdate(current=current, stale_paths=stale_paths, update_fields=update_fields)


async def refuse_provider_update(
    body: UpdateProviderRequest, name: str, request: Request, user: CurrentUser
) -> None:
    """FastAPI dependency: answer an update's own refusals before its re-auth grant is asked for."""
    _refuse_provider_update(body=body, name=name, request=request, user=user)


@router.put(
    "/{name}",
    response_model=ProviderResponse,
    dependencies=[
        Depends(require_own_org_action(scopes_dict["oidc_write"])),
        Depends(refuse_provider_update),
        Depends(require_provider_reauth),
    ],
)
async def update_provider(
    name: str,
    body: UpdateProviderRequest,
    user: CurrentUser,
    request: Request,
):
    """Update an OIDC provider configuration (admin only).

    A secret the body omits, value and env var name alike, keeps what the provider already holds, so an update that only flips ``enabled`` does not strand a stored credential. An empty env var name clears the name and keeps the stored copy. A secret the body names an env var for and sends no value for moves onto that env var and its stored copy is deleted once the update is saved, unless the name is the one already held with nothing stored. A move onto an env var the engine does not set is refused (``env_var_not_set``). Any update that touches more than ``enabled``, ``display_name`` and ``trusted_email_domains`` must leave the provider passing the field rules. Both are checked before a secret is written or deleted.
    """
    registry = _get_registry(request)
    plan = _refuse_provider_update(body=body, name=name, request=request, user=user)
    stale_paths, update_fields = plan.stale_paths, plan.update_fields

    if body.client_secret:
        secret = body.client_secret
        await run_blocking(
            lambda: _store_secret(field="client_secret", name=name, request=request, value=secret)
        )
    if body.groups is not None:
        groups = body.groups
        await run_blocking(lambda: _store_group_secrets(groups=groups, name=name, request=request))

    provider = registry.update(name, **update_fields)
    # Picks up an enable/disable flip and a rotated client secret alike.
    await _refresh_rp(request)
    audit_resource_change(user.user_id, "oidc_provider", name, "updated")
    # Deleted only once the saved config no longer points at them, so a failed update never leaves the provider on a missing secret.
    await run_blocking(lambda: _delete_secrets(paths=stale_paths, request=request))
    return _provider_to_response(name, provider)


@router.delete(
    "/{name}",
    response_model=DetachResponse,
    dependencies=[
        Depends(require_own_org_action(scopes_dict["oidc_delete"])),
        Depends(require_provider_reauth),
    ],
)
async def delete_provider(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Detach an OIDC provider and soft-delete its accounts (admin only).

    Does NOT delete groups -- they become orphaned with their source_provider
    still set to the deleted provider name. Accounts owned by the provider
    (``source_provider``) or last logged in through it (``oidc_id``) are
    soft-deleted: disabled and renamed to ``{uuid}-deleted`` with contact fields
    cleared. A sign-in linked through it to another account is unlinked and that
    account's sessions end, but the account stays. The provider's stored credentials
    ARE removed: nothing is left that can authenticate as a detached provider.
    """
    from dfe_engine.auth.accounts import soft_delete_account
    from dfe_engine.auth.groups import GroupStore
    from dfe_engine.auth.protected_accounts import ProtectedAccountError

    registry = _get_registry(request)
    provider = _require_reachable_provider(
        action=scopes_dict["oidc_delete"], name=name, request=request, user=user
    )

    # Find groups that reference this provider
    group_store: GroupStore = request.app.state.group_store
    orphaned = []
    for group in group_store.list():
        if group.source_provider == name:
            orphaned.append(
                OrphanedGroupInfo(
                    name=group.name,
                    source_provider=group.source_provider,
                    roles=group.roles,
                    member_count=len(group.members),
                )
            )

    # Soft-delete accounts tied to this provider before the YAML is gone. An org's provider reaches only that org's.
    account_store = request.app.state.account_store
    org = provider.org
    owned = [
        account
        for account in account_store.list()
        if ((account.source_provider == name) or (account.oidc_id == name))
        and (account.enabled)
        and (not (account.is_link_record))
        and ((not (org)) or (account.org == org))
    ]
    if (org) and (not (reads_every_org(request, user, scopes_dict["oidc_delete"]))):
        admins = usable_org_admins(
            accounts=account_store.list(),
            bindings=provider_bindings(request),
            groups=group_store.list(),
            org=org,
        )
        if (admins) and (set(admins) <= {account.username for account in owned}):
            raise ProblemError(
                409,
                f"Cannot delete provider {name!r}: every admin of org {org!r} signs in through it",
                code="last_org_admin",
            )
    # A sign-in linked through it leaves its account, which the provider does not own.
    unlink_provider(org=org, provider=name, store=account_store)
    disabled_accounts: list[DisabledAccountInfo] = []
    for account in owned:
        previous = account.username
        try:
            updated = soft_delete_account(account_store, previous)
        except ProtectedAccountError:
            # Recovery credentials stay usable. The detached IdP can no longer mint them.
            continue
        disabled_accounts.append(
            DisabledAccountInfo(
                username=updated.username,
                previous_username=updated.previous_username or previous,
                source_provider=updated.source_provider,
                oidc_id=updated.oidc_id,
            )
        )

    registry.delete(name)
    await run_blocking(lambda: _delete_stored_secrets(request, provider))
    # A detached provider must stop serving logins immediately, not at restart.
    await _refresh_rp(request)
    audit_resource_change(user.user_id, "oidc_provider", name, "deleted")

    return DetachResponse(
        deleted=name,
        orphaned_groups=orphaned,
        disabled_accounts=disabled_accounts,
    )


async def delete_org_providers(*, actor: str, org: str, request: Request) -> list[str]:
    """Delete every sign-in provider bound to *org*, with its stored secrets, once the org itself goes."""
    registry = _get_registry(request)
    bound = [(name, provider) for name, provider in registry.list() if provider.org == org]
    store = request.app.state.account_store
    for name, provider in bound:
        unlink_provider(org=org, provider=name, store=store)
        registry.delete(name)
        await run_blocking(functools.partial(_delete_stored_secrets, request, provider))
        audit_resource_change(actor, "oidc_provider", name, "deleted")
    if bound:
        # A deleted provider must stop serving sign-ins now, not at restart.
        await _refresh_rp(request)
    return [name for name, _ in bound]


@router.post(
    "/{name}/sync",
    response_model=SyncResponse,
    dependencies=[
        Depends(require_action(scopes_dict["oidc_write"])),
        Depends(require_provider_reauth),
    ],
)
async def sync_provider_groups(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Force group sync for an OIDC provider (admin only)."""
    from dfe_engine.auth.oidc.sync import sync_provider

    registry = _get_registry(request)
    if registry.get(name) is None:
        raise ProblemError(404, f"OIDC provider '{name}' not found", code="not_found")

    group_store = request.app.state.group_store
    result = await sync_provider(
        name,
        registry,
        group_store,
        secrets=getattr(request.app.state, "dfe_secrets", None),
        metrics=getattr(request.app.state, "oidc_sync_metrics", None),
        bindings=provider_bindings(request),
    )
    # A group the sync created is a new ClickHouse user to provision.
    if result["created"]:
        request_ch_rbac_reconcile(request.app.state)
    audit_resource_change(user.user_id, "oidc_provider", name, "executed")

    return SyncResponse(
        created=result["created"],
        updated=result["updated"],
        total=result["total"],
        groups_skipped=result["groups_skipped"],
        error=result.get("error"),
        skipped=result.get("skipped"),
        status=result["status"],
    )


@router.get(
    "/{name}/test",
    response_model=TestResponse,
    openapi_extra=scalo_extra(verb="test"),
    dependencies=[Depends(require_own_org_action(scopes_dict["oidc_read"]))],
)
async def test_provider(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Test connectivity to an OIDC provider (admin only)."""
    from dfe_engine.auth.oidc.adapters import get_adapter

    provider = _require_reachable_provider(
        action=scopes_dict["oidc_read"], name=name, request=request, user=user
    )

    adapter = get_adapter(provider, secrets=getattr(request.app.state, "dfe_secrets", None))
    success, message = await adapter.test_connection()
    return TestResponse(success=success, message=message)


@router.get(
    "/{name}/verify-login",
    response_model=LoginConfigResponse,
    openapi_extra=scalo_extra(verb="verify-login"),
    dependencies=[Depends(require_own_org_action(scopes_dict["oidc_read"]))],
)
async def verify_login_config(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Verify a provider's OIDC LOGIN config (admin only).

    Answers "are the login creds real and is the IdP reachable" WITHOUT
    attempting an interactive login - the gap ``/{name}/test`` leaves for
    generic providers. Checks: the client_id resolves, the client_secret
    resolves, and the issuer's discovery document is reachable and well-formed.
    """
    provider = _require_reachable_provider(
        action=scopes_dict["oidc_read"], name=name, request=request, user=user
    )

    from dfe_engine.auth.oidc.credential_env import credential_check

    store = getattr(request.app.state, "dfe_secrets", None)
    checks: list[LoginConfigCheck] = []

    # Resolve each credential the way the RP does and report only whether it
    # resolved - the value never enters the response. The store read blocks.
    for label, value, secret_path, env_name in (
        ("client_id", provider.client_id, "", provider.client_id_env),
        ("client_secret", "", provider.client_secret_path, provider.client_secret_env),
    ):
        ok, detail = await run_blocking(
            functools.partial(
                credential_check,
                label,
                value=value,
                secret_path=secret_path,
                env_name=env_name,
                secrets=store,
            )
        )
        checks.append(LoginConfigCheck(name=label, ok=ok, detail=detail))

    # Discovery document: reachable, JSON, and carries the endpoints Authlib needs.
    if not provider.issuer:
        checks.append(LoginConfigCheck(name="discovery", ok=False, detail="no issuer configured"))
    else:
        url = f"{provider.issuer.rstrip('/')}/.well-known/openid-configuration"
        from scalo.http import AsyncHttpClient

        try:
            async with AsyncHttpClient() as client:
                response = await client.get(url)
                data = response.json()
            missing = [
                k for k in ("issuer", "authorization_endpoint", "jwks_uri") if not data.get(k)
            ]
            if missing:
                checks.append(
                    LoginConfigCheck(
                        name="discovery",
                        ok=False,
                        detail=f"discovery reachable but missing {', '.join(missing)}",
                    )
                )
            else:
                checks.append(
                    LoginConfigCheck(name="discovery", ok=True, detail="discovery reachable")
                )
        except Exception as exc:
            reference = failure_reference()
            logger.warning(
                "OIDC verify-login: discovery failed",
                operation="discovery",
                provider=name,
                reference=reference,
                **describe_http_error(exc),
            )
            detail = referenced(
                http_failure_message(exc, service="The issuer's discovery endpoint"), reference
            )
            checks.append(LoginConfigCheck(name="discovery", ok=False, detail=detail))

    return LoginConfigResponse(ok=all(c.ok for c in checks), checks=checks)
