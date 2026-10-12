#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/field_rules_cases.py
#  Purpose:      Case tables for the per-type OIDC provider field rules
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the per-type OIDC provider field rules."""

from typing import TypedDict

from dfe_engine.auth.oidc.field_rules import FieldProblem
from dfe_engine.auth.oidc.models import OIDCProvider
from tests.unit.test_auth.factories import make_field_problem, make_oidc_provider

ENTRA_ISSUER = "https://login.microsoftonline.com/tid/v2.0"


class FieldProblemsCase(TypedDict):
    id: str
    expected_problems: list[FieldProblem]
    provider: OIDCProvider
    sent_group_fields: frozenset[str]


FIELD_PROBLEMS_CASES: list[FieldProblemsCase] = [
    {
        "id": "generic_token_claim",
        "expected_problems": [],
        "provider": make_oidc_provider(client_id="c"),
        "sent_group_fields": frozenset(),
    },
    {
        "id": "generic_manual",
        "expected_problems": [],
        "provider": make_oidc_provider(client_id_env="OIDC_CLIENT_ID", groups={"mode": "manual"}),
        "sent_group_fields": frozenset(),
    },
    {
        "id": "okta_api",
        "expected_problems": [],
        "provider": make_oidc_provider(
            client_id="c",
            groups={"api_token_env": "OKTA_TOKEN", "mode": "api", "okta_domain": "acme.okta.com"},
            type="okta",
        ),
        "sent_group_fields": frozenset({"api_token_env", "okta_domain"}),
    },
    {
        "id": "okta_api_with_enrich_on_login",
        "expected_problems": [],
        "provider": make_oidc_provider(
            client_id="c",
            groups={
                "api_token_path": "oidc/okta/groups_api_token",
                "enrich_on_login": True,
                "mode": "api",
                "okta_domain": "acme.okta.com",
            },
            type="okta",
        ),
        "sent_group_fields": frozenset({"api_token", "enrich_on_login", "okta_domain"}),
    },
    {
        "id": "entra_api_with_its_own_secret",
        "expected_problems": [],
        "provider": make_oidc_provider(
            client_id="c",
            groups={"client_secret_env": "ENTRA_SECRET", "mode": "api", "tenant_id": "tid"},
            issuer=ENTRA_ISSUER,
            type="entra_id",
        ),
        "sent_group_fields": frozenset({"client_secret_env", "tenant_id"}),
    },
    {
        "id": "entra_api_falls_back_to_the_login_secret",
        "expected_problems": [],
        "provider": make_oidc_provider(
            client_id="c",
            client_secret_path="oidc/entra/client_secret",
            groups={"mode": "api", "tenant_id_env": "ENTRA_TENANT"},
            issuer=ENTRA_ISSUER,
            type="entra_id",
        ),
        "sent_group_fields": frozenset({"tenant_id_env"}),
    },
    {
        "id": "entra_token_claim_needs_no_directory_credentials",
        "expected_problems": [],
        "provider": make_oidc_provider(client_id="c", issuer=ENTRA_ISSUER, type="entra_id"),
        "sent_group_fields": frozenset(),
    },
    {
        "id": "entra_token_claim_may_carry_overage_credentials",
        "expected_problems": [],
        "provider": make_oidc_provider(
            client_id="c",
            groups={"client_secret_env": "ENTRA_SECRET", "tenant_id": "tid"},
            issuer=ENTRA_ISSUER,
            type="entra_id",
        ),
        "sent_group_fields": frozenset({"client_secret_env", "tenant_id"}),
    },
    {
        "id": "google_api_needs_no_service_account",
        "expected_problems": [],
        "provider": make_oidc_provider(
            client_id="c",
            groups={"enrich_on_login": True, "mode": "api"},
            issuer="https://accounts.google.com",
            type="google",
        ),
        "sent_group_fields": frozenset({"enrich_on_login"}),
    },
    {
        "id": "google_api_with_a_service_account",
        "expected_problems": [],
        "provider": make_oidc_provider(
            client_id="c",
            groups={
                "domain": "acme.com",
                "enrich_on_login": True,
                "mode": "api",
                "service_account_json_path": "oidc/google/groups_service_account_json",
            },
            issuer="https://accounts.google.com",
            type="google",
        ),
        "sent_group_fields": frozenset({"domain", "enrich_on_login", "service_account_json"}),
    },
    {
        "id": "missing_issuer",
        "expected_problems": [
            make_field_problem(code="missing", field="issuer", message="required")
        ],
        "provider": make_oidc_provider(client_id="c", issuer=""),
        "sent_group_fields": frozenset(),
    },
    {
        "id": "missing_client_id",
        "expected_problems": [
            make_field_problem(
                code="missing",
                field="client_id",
                message="required: send client_id or client_id_env",
            )
        ],
        "provider": make_oidc_provider(),
        "sent_group_fields": frozenset(),
    },
    {
        "id": "generic_refuses_api_mode",
        "expected_problems": [
            make_field_problem(
                code="not_allowed",
                field="groups.mode",
                message="a 'generic' provider supports only these modes: 'manual', 'token_claim'",
            )
        ],
        "provider": make_oidc_provider(client_id="c", groups={"mode": "api"}),
        "sent_group_fields": frozenset(),
    },
    {
        "id": "google_refuses_token_claim_mode",
        "expected_problems": [
            make_field_problem(
                code="not_allowed",
                field="groups.mode",
                message="a 'google' provider supports only these modes: 'api'",
            )
        ],
        "provider": make_oidc_provider(
            client_id="c",
            groups={"domain": "acme.com"},
            issuer="https://accounts.google.com",
            type="google",
        ),
        "sent_group_fields": frozenset({"domain"}),
    },
    {
        "id": "okta_api_without_domain_or_token",
        "expected_problems": [
            make_field_problem(
                code="missing",
                field="groups.okta_domain",
                message="required for an 'okta' provider in 'api' mode",
            ),
            make_field_problem(
                code="missing",
                field="groups.api_token",
                message="required for an 'okta' provider in 'api' mode: send api_token or api_token_env",
            ),
        ],
        "provider": make_oidc_provider(client_id="c", groups={"mode": "api"}, type="okta"),
        "sent_group_fields": frozenset(),
    },
    {
        "id": "entra_api_without_tenant_or_any_secret",
        "expected_problems": [
            make_field_problem(
                code="missing",
                field="groups.tenant_id",
                message="required for an 'entra_id' provider in 'api' mode: send tenant_id or tenant_id_env",
            ),
            make_field_problem(
                code="missing",
                field="groups.client_secret",
                message="required for an 'entra_id' provider in 'api' mode: send client_secret or client_secret_env, here or on the login client",
            ),
        ],
        "provider": make_oidc_provider(
            client_id="c", groups={"mode": "api"}, issuer=ENTRA_ISSUER, type="entra_id"
        ),
        "sent_group_fields": frozenset(),
    },
    {
        "id": "google_api_without_enrichment",
        "expected_problems": [
            make_field_problem(
                code="not_allowed",
                field="groups.enrich_on_login",
                message="must be on: a 'google' provider's tokens carry no groups",
            ),
        ],
        "provider": make_oidc_provider(
            client_id="c",
            groups={"mode": "api"},
            issuer="https://accounts.google.com",
            type="google",
        ),
        "sent_group_fields": frozenset(),
    },
    {
        "id": "refuses_another_types_field",
        "expected_problems": [
            make_field_problem(
                code="not_allowed",
                field="groups.okta_domain",
                message="not used by an 'entra_id' provider in 'token_claim' mode",
            )
        ],
        "provider": make_oidc_provider(
            client_id="c",
            groups={"okta_domain": "acme.okta.com"},
            issuer=ENTRA_ISSUER,
            type="entra_id",
        ),
        "sent_group_fields": frozenset({"okta_domain"}),
    },
    {
        "id": "refuses_a_directory_field_in_manual_mode",
        "expected_problems": [
            make_field_problem(
                code="not_allowed",
                field="groups.tenant_id",
                message="not used by an 'entra_id' provider in 'manual' mode",
            )
        ],
        "provider": make_oidc_provider(
            client_id="c",
            groups={"mode": "manual", "tenant_id": "tid"},
            issuer=ENTRA_ISSUER,
            type="entra_id",
        ),
        "sent_group_fields": frozenset({"tenant_id"}),
    },
    {
        "id": "refuses_enrich_on_login_outside_okta_and_google",
        "expected_problems": [
            make_field_problem(
                code="not_allowed",
                field="groups.enrich_on_login",
                message="not used by an 'entra_id' provider in 'api' mode",
            )
        ],
        "provider": make_oidc_provider(
            client_id="c",
            groups={
                "client_secret_env": "ENTRA_SECRET",
                "enrich_on_login": True,
                "mode": "api",
                "tenant_id": "tid",
            },
            issuer=ENTRA_ISSUER,
            type="entra_id",
        ),
        "sent_group_fields": frozenset({"client_secret_env", "enrich_on_login", "tenant_id"}),
    },
    {
        "id": "refuses_a_directory_token_on_a_generic_provider",
        "expected_problems": [
            make_field_problem(
                code="not_allowed",
                field="groups.api_token",
                message="not used by a 'generic' provider in 'token_claim' mode",
            ),
            make_field_problem(
                code="not_allowed",
                field="groups.api_token_env",
                message="not used by a 'generic' provider in 'token_claim' mode",
            ),
        ],
        "provider": make_oidc_provider(client_id="c"),
        "sent_group_fields": frozenset({"api_token", "api_token_env"}),
    },
    {
        "id": "refuses_google_fields_on_okta",
        "expected_problems": [
            make_field_problem(
                code="not_allowed",
                field=f"groups.{field}",
                message="not used by an 'okta' provider in 'api' mode",
            )
            for field in (
                "domain",
                "service_account_json",
                "service_account_json_env",
            )
        ],
        "provider": make_oidc_provider(
            client_id="c",
            groups={"api_token_env": "OKTA_TOKEN", "mode": "api", "okta_domain": "acme.okta.com"},
            type="okta",
        ),
        "sent_group_fields": frozenset(
            {
                "api_token_env",
                "domain",
                "okta_domain",
                "service_account_json",
                "service_account_json_env",
            }
        ),
    },
    {
        "id": "refuses_entra_credentials_in_manual_mode",
        "expected_problems": [
            make_field_problem(
                code="not_allowed",
                field=f"groups.{field}",
                message="not used by an 'entra_id' provider in 'manual' mode",
            )
            for field in ("client_secret", "client_secret_env", "tenant_id_env")
        ],
        "provider": make_oidc_provider(
            client_id="c", groups={"mode": "manual"}, issuer=ENTRA_ISSUER, type="entra_id"
        ),
        "sent_group_fields": frozenset({"client_secret", "client_secret_env", "tenant_id_env"}),
    },
    {
        "id": "a_stored_field_not_sent_is_not_refused",
        "expected_problems": [],
        "provider": make_oidc_provider(
            client_id="c",
            groups={
                "client_secret_path": "oidc/entra/groups_client_secret",
                "mode": "manual",
                "tenant_id": "tid",
            },
            issuer=ENTRA_ISSUER,
            type="entra_id",
        ),
        "sent_group_fields": frozenset(),
    },
]
