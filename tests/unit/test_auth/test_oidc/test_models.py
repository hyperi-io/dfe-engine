#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_models.py
#  Purpose:      Tests for OIDCProvider, GroupResolutionConfig, and GroupInfo models
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import pytest
from pydantic import ValidationError

from dfe_engine.auth.oidc.models import GroupInfo, GroupResolutionConfig, OIDCProvider


class TestGroupResolutionConfig:
    def test_defaults(self):
        cfg = GroupResolutionConfig()
        assert cfg.mode == "token_claim"
        assert cfg.claim_name == "groups"
        assert cfg.sync_interval == 3600
        assert cfg.service_account_json_env == ""
        assert cfg.domain == ""
        assert cfg.tenant_id_env == ""
        assert cfg.client_secret_env == ""
        assert cfg.api_token_env == ""
        assert cfg.okta_domain == ""

    def test_valid_modes(self):
        for mode in ("manual", "token_claim", "api"):
            cfg = GroupResolutionConfig(mode=mode)
            assert cfg.mode == mode

    def test_invalid_mode_raises(self):
        with pytest.raises(ValidationError):
            GroupResolutionConfig(mode="ldap")

    def test_token_claim_mode_with_custom_claim(self):
        cfg = GroupResolutionConfig(mode="token_claim", claim_name="cognito:groups")
        assert cfg.claim_name == "cognito:groups"

    def test_google_fields(self):
        cfg = GroupResolutionConfig(
            mode="api",
            service_account_json_env="GOOGLE_SA_JSON",
            domain="example.com",
        )
        assert cfg.service_account_json_env == "GOOGLE_SA_JSON"
        assert cfg.domain == "example.com"

    def test_a_stored_admin_email_still_loads(self):
        # Provider files written before impersonation was removed carry admin_email.
        cfg = GroupResolutionConfig.model_validate({"admin_email": "a@example.com", "mode": "api"})
        assert cfg.mode == "api"
        assert "admin_email" not in cfg.model_dump()

    def test_entra_fields(self):
        cfg = GroupResolutionConfig(
            mode="api",
            tenant_id_env="AZURE_TENANT_ID",
            client_secret_env="AZURE_CLIENT_SECRET",
        )
        assert cfg.tenant_id_env == "AZURE_TENANT_ID"
        assert cfg.client_secret_env == "AZURE_CLIENT_SECRET"

    def test_okta_fields(self):
        cfg = GroupResolutionConfig(
            mode="api",
            api_token_env="OKTA_API_TOKEN",
            okta_domain="dev-12345.okta.com",
        )
        assert cfg.api_token_env == "OKTA_API_TOKEN"
        assert cfg.okta_domain == "dev-12345.okta.com"

    def test_custom_sync_interval(self):
        cfg = GroupResolutionConfig(sync_interval=300)
        assert cfg.sync_interval == 300


class TestOIDCProviderDefaults:
    def test_defaults(self):
        provider = OIDCProvider()
        assert provider.type == "generic"
        assert provider.enabled is True
        assert provider.display_name == ""
        assert provider.issuer == ""
        assert provider.client_id_env == ""
        assert provider.created_at == ""
        assert provider.last_sync_at == ""
        assert provider.last_sync_status == ""
        assert provider.sync_error == ""

    def test_default_groups_resolution_config(self):
        provider = OIDCProvider()
        assert isinstance(provider.groups, GroupResolutionConfig)
        assert provider.groups.mode == "token_claim"


GOOGLE_SCOPES = (
    "openid email profile https://www.googleapis.com/auth/cloud-identity.groups.readonly"
)


class TestOIDCProviderScopes:
    """Each provider type requests only the scopes its IdP accepts.

    Google and Entra ID advertise no ``groups`` scope and fail the login that asks
    for one, and Google reads groups with the login's Cloud Identity scope; dex
    (consumed as generic) and Okta emit the groups claim only when it is requested.
    """

    @pytest.mark.parametrize(
        ("provider_type", "expected"),
        [
            ("google", GOOGLE_SCOPES),
            ("entra_id", "openid email profile"),
            ("generic", "openid email profile groups"),
            ("okta", "openid email profile groups"),
        ],
    )
    def test_default_scopes_follow_the_type(self, provider_type, expected):
        assert OIDCProvider(type=provider_type).scopes == expected

    @pytest.mark.parametrize("provider_type", ["google", "entra_id"])
    def test_providers_without_a_groups_scope_never_request_one(self, provider_type):
        assert "groups" not in OIDCProvider(type=provider_type).scopes.split()

    def test_a_stored_provider_without_scopes_loads_its_type_default(self):
        provider = OIDCProvider.model_validate({"type": "google", "issuer": "https://x"})
        assert provider.scopes == GOOGLE_SCOPES

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_scopes_take_the_type_default(self, blank):
        assert OIDCProvider(type="google", scopes=blank).scopes == GOOGLE_SCOPES

    def test_configured_scopes_are_kept(self):
        provider = OIDCProvider(type="google", scopes="openid email")
        assert provider.scopes == "openid email"


class TestOIDCProviderTypes:
    def test_generic_provider(self):
        provider = OIDCProvider(
            type="generic",
            issuer="https://sso.example.com",
            client_id_env="OIDC_CLIENT_ID",
        )
        assert provider.type == "generic"

    def test_google_provider(self):
        provider = OIDCProvider(
            type="google",
            display_name="Google Workspace",
            issuer="https://accounts.google.com",
            client_id_env="GOOGLE_CLIENT_ID",
            groups=GroupResolutionConfig(
                mode="api",
                service_account_json_env="GOOGLE_SA_JSON",
                domain="example.com",
            ),
        )
        assert provider.type == "google"
        assert provider.groups.mode == "api"
        assert provider.groups.domain == "example.com"

    def test_entra_id_provider(self):
        provider = OIDCProvider(
            type="entra_id",
            display_name="Microsoft Entra ID",
            issuer="https://login.microsoftonline.com/tenant-id/v2.0",
            client_id_env="AZURE_CLIENT_ID",
            groups=GroupResolutionConfig(
                mode="api",
                tenant_id_env="AZURE_TENANT_ID",
                client_secret_env="AZURE_CLIENT_SECRET",
            ),
        )
        assert provider.type == "entra_id"
        assert provider.groups.tenant_id_env == "AZURE_TENANT_ID"

    def test_okta_provider(self):
        provider = OIDCProvider(
            type="okta",
            display_name="Okta",
            issuer="https://dev-12345.okta.com",
            client_id_env="OKTA_CLIENT_ID",
            groups=GroupResolutionConfig(
                mode="api",
                api_token_env="OKTA_API_TOKEN",
                okta_domain="dev-12345.okta.com",
            ),
        )
        assert provider.type == "okta"
        assert provider.groups.okta_domain == "dev-12345.okta.com"

    def test_invalid_type_raises(self):
        with pytest.raises(ValidationError):
            OIDCProvider(type="saml")

    def test_disabled_provider(self):
        provider = OIDCProvider(enabled=False)
        assert provider.enabled is False


class TestGroupInfo:
    def test_minimal_construction(self):
        gi = GroupInfo(id="grp-001", name="Engineering")
        assert gi.id == "grp-001"
        assert gi.name == "Engineering"
        assert gi.email == ""
        assert gi.description == ""

    def test_full_construction(self):
        gi = GroupInfo(
            id="grp-001",
            name="Engineering",
            email="engineering@example.com",
            description="The engineering team",
        )
        assert gi.email == "engineering@example.com"
        assert gi.description == "The engineering team"

    def test_id_required(self):
        with pytest.raises(ValidationError):
            GroupInfo(name="No ID")

    def test_name_required(self):
        with pytest.raises(ValidationError):
            GroupInfo(id="grp-001")
