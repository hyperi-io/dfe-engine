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
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry


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

    def test_a_stored_admin_email_still_loads(self):
        # Provider files written before impersonation was removed carry admin_email.
        cfg = GroupResolutionConfig.model_validate({"admin_email": "a@example.com", "mode": "api"})
        assert cfg.mode == "api"
        assert "admin_email" not in cfg.model_dump()


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
        assert provider.groups == GroupResolutionConfig()


GOOGLE_SCOPES = (
    "openid email profile https://www.googleapis.com/auth/cloud-identity.groups.readonly"
)


class TestOIDCProviderScopes:
    """Each provider type requests only the scopes its IdP accepts.

    Google and Entra ID advertise no ``groups`` scope and fail the login that asks
    for one, and Google reads groups with the login's Cloud Identity scope. Dex
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

    def test_google_and_entra_providers_holding_the_old_default_are_repaired(self):
        # Every provider saved before defaults followed the type stored the old default.
        stored = {"issuer": "https://x", "scopes": "groups profile openid email"}
        loaded = {
            provider_type: OIDCProvider.model_validate({**stored, "type": provider_type}).scopes
            for provider_type in ("google", "entra_id", "okta")
        }
        assert loaded == {
            "entra_id": "openid email profile",
            "google": GOOGLE_SCOPES,
            "okta": "groups profile openid email",
        }

    def test_configured_scopes_are_kept(self):
        provider = OIDCProvider(type="google", scopes="openid email")
        assert provider.scopes == "openid email"


class TestRefusedValues:
    @pytest.mark.parametrize(
        ("model", "fields"),
        [
            (GroupResolutionConfig, {"mode": "ldap"}),
            (OIDCProvider, {"type": "saml"}),
            (GroupInfo, {"name": "No ID"}),
            (GroupInfo, {"id": "grp-001"}),
        ],
        ids=["unknown-mode", "unknown-type", "group-without-id", "group-without-name"],
    )
    def test_refused(self, model, fields):
        with pytest.raises(ValidationError):
            model.model_validate(fields)


class TestAGenericProviderReadsNoDirectory:
    """It has no directory API, so asking one at login would leave every user with no groups."""

    def test_enrich_on_login_in_api_mode_is_refused(self):
        with pytest.raises(ValidationError, match="no directory API"):
            OIDCProvider(type="generic", groups={"enrich_on_login": True, "mode": "api"})

    @pytest.mark.parametrize(
        "groups",
        [{"mode": "api"}, {"enrich_on_login": True, "mode": "token_claim"}],
        ids=["api-mode-alone", "enrich-outside-api-mode"],
    )
    def test_either_setting_alone_still_loads(self, groups):
        assert OIDCProvider(type="generic", groups=groups).type == "generic"

    @pytest.mark.parametrize("provider_type", ["google", "okta", "entra_id"])
    def test_a_provider_with_a_directory_may_enrich(self, provider_type):
        groups = {"enrich_on_login": True, "mode": "api"}
        assert OIDCProvider(type=provider_type, groups=groups).groups.enrich_on_login is True

    def test_a_provider_file_written_past_the_api_does_not_load(self, tmp_path):
        directory = tmp_path / "oidc"
        directory.mkdir()
        (directory / "generic-enrich.yaml").write_text(
            "type: generic\nissuer: https://idp.example.com\n"
            "groups:\n  mode: api\n  enrich_on_login: true\n",
            encoding="utf-8",
        )
        registry = OIDCProviderRegistry(directory)

        assert registry.list() == []
        with pytest.raises(ValidationError):
            registry.get("generic-enrich")


class TestGroupInfo:
    def test_minimal_construction(self):
        gi = GroupInfo(id="grp-001", name="Engineering")
        assert (gi.id, gi.name, gi.email, gi.description) == ("grp-001", "Engineering", "", "")
