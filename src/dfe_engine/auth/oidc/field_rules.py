#  Project:      dfe-engine
#  File:         auth/oidc/field_rules.py
#  Purpose:      Which fields each OIDC provider type and group mode accepts and requires
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Which fields each OIDC provider type and group mode accepts and requires.

The provider admin API checks a provider against these rules before it writes anything. The registry does not, so a provider written out of band still loads.
"""

from dataclasses import dataclass

from dfe_engine.auth.oidc.models import OIDCProvider

_MODES_BY_TYPE = {
    "entra_id": ("manual", "token_claim", "api"),
    "generic": ("manual", "token_claim"),
    "google": ("api",),
    "okta": ("manual", "token_claim", "api"),
}

# The (type, mode) pairs that use each directory field; every other pair refuses it.
_DIRECTORY_FIELD_USERS = {
    "admin_email": {("google", "api")},
    "api_token": {("okta", "api")},
    "api_token_env": {("okta", "api")},
    "client_secret": {("entra_id", "api"), ("entra_id", "token_claim")},
    "client_secret_env": {("entra_id", "api"), ("entra_id", "token_claim")},
    "domain": {("google", "api")},
    "enrich_on_login": {("google", "api"), ("okta", "api")},
    "okta_domain": {("okta", "api")},
    "service_account_json": {("google", "api")},
    "service_account_json_env": {("google", "api")},
    "tenant_id": {("entra_id", "api"), ("entra_id", "token_claim")},
    "tenant_id_env": {("entra_id", "api"), ("entra_id", "token_claim")},
}

_VOWELS = ("a", "e", "i", "o", "u")


@dataclass(frozen=True, slots=True)
class FieldProblem:
    """One provider field the rules refuse: its dotted name, why and whether it is missing or not allowed."""

    code: str
    field: str
    message: str


def _api_mode_problems(*, provider: OIDCProvider) -> list[FieldProblem]:
    """The directory fields an api-mode provider of this type needs and lacks."""
    groups = provider.groups
    required = (
        f"required for {_article(word=provider.type)} {provider.type!r} provider in 'api' mode"
    )
    problems = []
    match provider.type:
        case "okta":
            if not (groups.okta_domain):
                problems.append(
                    FieldProblem(code="missing", field="groups.okta_domain", message=required)
                )
            if not (groups.api_token_path) and not (groups.api_token_env):
                message = f"{required}: send api_token or api_token_env"
                problems.append(
                    FieldProblem(code="missing", field="groups.api_token", message=message)
                )
        case "entra_id":
            if not (groups.tenant_id) and not (groups.tenant_id_env):
                message = f"{required}: send tenant_id or tenant_id_env"
                problems.append(
                    FieldProblem(code="missing", field="groups.tenant_id", message=message)
                )
            has_group_secret = (groups.client_secret_path) or (groups.client_secret_env)
            has_login_secret = (provider.client_secret_path) or (provider.client_secret_env)
            if not (has_group_secret) and not (has_login_secret):
                message = f"{required}: send client_secret or client_secret_env, here or on the login client"
                problems.append(
                    FieldProblem(code="missing", field="groups.client_secret", message=message)
                )
        case "google":
            if not (groups.service_account_json_path) and not (groups.service_account_json_env):
                message = f"{required}: send service_account_json or service_account_json_env"
                problems.append(
                    FieldProblem(
                        code="missing", field="groups.service_account_json", message=message
                    )
                )
            if not (groups.admin_email):
                problems.append(
                    FieldProblem(code="missing", field="groups.admin_email", message=required)
                )
            if not (groups.enrich_on_login):
                message = "must be on: a 'google' provider's tokens carry no groups"
                problems.append(
                    FieldProblem(
                        code="not_allowed", field="groups.enrich_on_login", message=message
                    )
                )
    return problems


def _article(*, word: str) -> str:
    """The indefinite article that reads right before *word*."""
    return "an" if word[:1] in _VOWELS else "a"


def _login_problems(*, provider: OIDCProvider) -> list[FieldProblem]:
    """The login fields every provider needs and this one lacks."""
    problems = []
    if not (provider.issuer):
        problems.append(FieldProblem(code="missing", field="issuer", message="required"))
    if not (provider.client_id) and not (provider.client_id_env):
        message = "required: send client_id or client_id_env"
        problems.append(FieldProblem(code="missing", field="client_id", message=message))
    return problems


def _refused_problems(
    *, provider: OIDCProvider, sent_group_fields: frozenset[str]
) -> list[FieldProblem]:
    """The directory fields the caller sent that this provider's type and mode never use."""
    pair = (provider.type, provider.groups.mode)
    refused = sorted(
        field
        for field in sent_group_fields
        if field in _DIRECTORY_FIELD_USERS and pair not in _DIRECTORY_FIELD_USERS[field]
    )
    message = f"not used by {_article(word=provider.type)} {provider.type!r} provider in {provider.groups.mode!r} mode"
    return [
        FieldProblem(code="not_allowed", field=f"groups.{field}", message=message)
        for field in refused
    ]


def field_problems(
    *, provider: OIDCProvider, sent_group_fields: frozenset[str]
) -> list[FieldProblem]:
    """Every field rule *provider* breaks, in a stable order.

    *sent_group_fields* names the groups fields the caller set in this request, so a value already stored on the provider is never refused just because the mode changed.
    """
    problems = _login_problems(provider=provider)
    allowed_modes = _MODES_BY_TYPE[provider.type]
    if provider.groups.mode not in allowed_modes:
        modes = ", ".join(repr(mode) for mode in allowed_modes)
        message = f"{_article(word=provider.type)} {provider.type!r} provider supports only these modes: {modes}"
        problems.append(FieldProblem(code="not_allowed", field="groups.mode", message=message))
        return problems
    if provider.groups.mode == "api":
        problems.extend(_api_mode_problems(provider=provider))
    problems.extend(_refused_problems(provider=provider, sent_group_fields=sent_group_fields))
    return problems
