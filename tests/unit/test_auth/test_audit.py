#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_audit.py
#  Purpose:      Tests that audit functions emit correct structured log events.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for SOC2 audit event emitters.

Mocking the logger IS acceptable here — we are testing that the correct log
function is called with the correct structured arguments. This is one of the
explicit exceptions to the no-mocks policy for audit logging.
"""

from unittest.mock import patch

import pytest

from dfe_engine.auth.audit import (
    audit_account_change,
    audit_api_key_change,
    audit_group_change,
    audit_jit_account_created,
    audit_jit_failed,
    audit_jit_groups_updated,
    audit_jit_team_assigned,
    audit_login_denied,
    audit_login_success,
    audit_org_ch_failed,
    audit_org_ch_provisioned,
    audit_org_change,
    audit_org_hyperdx_failed,
    audit_org_hyperdx_provisioned,
    audit_permission_denied,
    audit_resource_change,
)


class TestAuditLoginSuccess:
    @patch("dfe_engine.auth.audit.logger")
    def test_emits_info_event(self, mock_logger):
        audit_login_success(
            user_id="alice",
            auth_path="oidc",
            client_ip="10.0.0.1",
            roles=["admin"],
        )
        mock_logger.info.assert_called_once()

    @patch("dfe_engine.auth.audit.logger")
    def test_event_name(self, mock_logger):
        audit_login_success(
            user_id="alice",
            auth_path="oidc",
            client_ip="10.0.0.1",
            roles=["admin"],
        )
        args, _ = mock_logger.info.call_args
        assert args[0] == "auth.login.success"

    @patch("dfe_engine.auth.audit.logger")
    def test_structured_fields(self, mock_logger):
        audit_login_success(
            user_id="alice",
            auth_path="jwt",
            client_ip="192.168.1.1",
            roles=["data_analyst", "viewer"],
        )
        _, kwargs = mock_logger.info.call_args
        assert kwargs["user_id"] == "alice"
        assert kwargs["auth_path"] == "jwt"
        assert kwargs["client_ip"] == "192.168.1.1"
        assert kwargs["roles"] == ["data_analyst", "viewer"]

    @patch("dfe_engine.auth.audit.logger")
    def test_none_client_ip_allowed(self, mock_logger):
        audit_login_success(
            user_id="alice",
            auth_path="oidc",
            client_ip=None,
            roles=[],
        )
        _, kwargs = mock_logger.info.call_args
        assert kwargs["client_ip"] is None

    @patch("dfe_engine.auth.audit.logger")
    def test_api_key_user_id_format(self, mock_logger):
        audit_login_success(
            user_id="apikey:ci-pipeline",
            auth_path="api_key",
            client_ip="10.0.0.2",
            roles=["infra_viewer"],
        )
        _, kwargs = mock_logger.info.call_args
        assert kwargs["user_id"] == "apikey:ci-pipeline"
        assert kwargs["auth_path"] == "api_key"


class TestAuditLoginDenied:
    @patch("dfe_engine.auth.audit.logger")
    def test_emits_warning_event(self, mock_logger):
        audit_login_denied(
            user_id="anonymous",
            auth_path="none",
            client_ip="10.0.0.1",
            reason="no_credentials",
        )
        mock_logger.warning.assert_called_once()
        mock_logger.info.assert_not_called()

    @patch("dfe_engine.auth.audit.logger")
    def test_event_name(self, mock_logger):
        audit_login_denied(
            user_id="anonymous",
            auth_path="none",
            client_ip="10.0.0.1",
            reason="no_credentials",
        )
        args, _ = mock_logger.warning.call_args
        assert args[0] == "auth.login.denied"

    @patch("dfe_engine.auth.audit.logger")
    def test_structured_fields(self, mock_logger):
        audit_login_denied(
            user_id="ABCDEF1234567890...",
            auth_path="api_key",
            client_ip="172.16.0.1",
            reason="invalid_key",
        )
        _, kwargs = mock_logger.warning.call_args
        assert kwargs["user_id"] == "ABCDEF1234567890..."
        assert kwargs["auth_path"] == "api_key"
        assert kwargs["client_ip"] == "172.16.0.1"
        assert kwargs["reason"] == "invalid_key"

    @patch("dfe_engine.auth.audit.logger")
    def test_jwt_invalid_reason(self, mock_logger):
        audit_login_denied(
            user_id="unknown",
            auth_path="jwt",
            client_ip=None,
            reason="Signature verification failed",
        )
        _, kwargs = mock_logger.warning.call_args
        assert kwargs["reason"] == "Signature verification failed"
        assert kwargs["auth_path"] == "jwt"


class TestAuditPermissionDenied:
    @patch("dfe_engine.auth.audit.logger")
    def test_emits_warning_event(self, mock_logger):
        audit_permission_denied(
            user_id="alice",
            action="source:write",
            roles=["viewer"],
            reason="No matching permission",
        )
        mock_logger.warning.assert_called_once()
        mock_logger.info.assert_not_called()

    @patch("dfe_engine.auth.audit.logger")
    def test_event_name(self, mock_logger):
        audit_permission_denied(
            user_id="alice",
            action="source:write",
            roles=["viewer"],
            reason="No matching permission",
        )
        args, _ = mock_logger.warning.call_args
        assert args[0] == "auth.permission.denied"

    @patch("dfe_engine.auth.audit.logger")
    def test_structured_fields(self, mock_logger):
        audit_permission_denied(
            user_id="bob",
            action="admin:write",
            roles=["data_analyst"],
            reason="Role data_analyst lacks admin:write",
        )
        _, kwargs = mock_logger.warning.call_args
        assert kwargs["user_id"] == "bob"
        assert kwargs["action"] == "admin:write"
        assert kwargs["roles"] == ["data_analyst"]
        assert "admin:write" in kwargs["reason"]


class TestAuditAccountChange:
    @pytest.mark.parametrize("change", ["created", "updated", "deleted"])
    @patch("dfe_engine.auth.audit.logger")
    def test_emits_info_event(self, mock_logger, change):
        audit_account_change(
            admin_id="admin",
            target_user="newuser",
            change=change,
        )
        mock_logger.info.assert_called_once()

    @pytest.mark.parametrize("change", ["created", "updated", "deleted"])
    @patch("dfe_engine.auth.audit.logger")
    def test_event_name_includes_change(self, mock_logger, change):
        audit_account_change(
            admin_id="admin",
            target_user="newuser",
            change=change,
        )
        args, _ = mock_logger.info.call_args
        assert args[0] == f"auth.account.{change}"

    @patch("dfe_engine.auth.audit.logger")
    def test_structured_fields(self, mock_logger):
        audit_account_change(
            admin_id="superadmin",
            target_user="alice",
            change="created",
        )
        _, kwargs = mock_logger.info.call_args
        assert kwargs["admin_id"] == "superadmin"
        assert kwargs["target_user"] == "alice"
        assert kwargs["change"] == "created"


class TestAuditGroupChange:
    @pytest.mark.parametrize("change", ["created", "updated", "deleted"])
    @patch("dfe_engine.auth.audit.logger")
    def test_emits_info_event(self, mock_logger, change):
        audit_group_change(
            admin_id="admin",
            group_name="soc-analysts",
            change=change,
        )
        mock_logger.info.assert_called_once()

    @pytest.mark.parametrize("change", ["created", "updated", "deleted"])
    @patch("dfe_engine.auth.audit.logger")
    def test_event_name_includes_change(self, mock_logger, change):
        audit_group_change(
            admin_id="admin",
            group_name="soc-analysts",
            change=change,
        )
        args, _ = mock_logger.info.call_args
        assert args[0] == f"auth.group.{change}"

    @patch("dfe_engine.auth.audit.logger")
    def test_structured_fields(self, mock_logger):
        audit_group_change(
            admin_id="superadmin",
            group_name="threat-hunters",
            change="updated",
        )
        _, kwargs = mock_logger.info.call_args
        assert kwargs["admin_id"] == "superadmin"
        assert kwargs["group_name"] == "threat-hunters"
        assert kwargs["change"] == "updated"


class TestAuditApiKeyChange:
    @pytest.mark.parametrize("change", ["created", "revoked"])
    @patch("dfe_engine.auth.audit.logger")
    def test_emits_info_event(self, mock_logger, change):
        audit_api_key_change(
            admin_id="admin",
            key_name="ci-pipeline",
            short_token="ABCDEF1234567890",
            change=change,
        )
        mock_logger.info.assert_called_once()

    @pytest.mark.parametrize("change", ["created", "revoked"])
    @patch("dfe_engine.auth.audit.logger")
    def test_event_name_includes_change(self, mock_logger, change):
        audit_api_key_change(
            admin_id="admin",
            key_name="ci-pipeline",
            short_token="ABCDEF1234567890",
            change=change,
        )
        args, _ = mock_logger.info.call_args
        assert args[0] == f"auth.api_key.{change}"

    @patch("dfe_engine.auth.audit.logger")
    def test_structured_fields(self, mock_logger):
        audit_api_key_change(
            admin_id="superadmin",
            key_name="deploy-bot",
            short_token="DEADBEEF12345678",
            change="revoked",
        )
        _, kwargs = mock_logger.info.call_args
        assert kwargs["admin_id"] == "superadmin"
        assert kwargs["key_name"] == "deploy-bot"
        assert kwargs["short_token"] == "DEADBEEF12345678"
        assert kwargs["change"] == "revoked"


class TestAuditOrgChange:
    @pytest.mark.parametrize("change", ["created", "updated", "deleted"])
    @patch("dfe_engine.auth.audit.logger")
    def test_emits_info_event(self, mock_logger, change):
        audit_org_change(admin_id="admin", org_name="acme", change=change)
        mock_logger.info.assert_called_once()

    @pytest.mark.parametrize("change", ["created", "updated", "deleted"])
    @patch("dfe_engine.auth.audit.logger")
    def test_event_name_includes_change(self, mock_logger, change):
        audit_org_change(admin_id="admin", org_name="acme", change=change)
        args, _ = mock_logger.info.call_args
        assert args[0] == f"org.{change}"

    @patch("dfe_engine.auth.audit.logger")
    def test_structured_fields(self, mock_logger):
        audit_org_change(
            admin_id="superadmin",
            org_name="acme",
            change="created",
            details={"plan": "enterprise"},
        )
        _, kwargs = mock_logger.info.call_args
        assert kwargs["admin_id"] == "superadmin"
        assert kwargs["org_name"] == "acme"
        assert kwargs["change"] == "created"
        assert kwargs["details"] == {"plan": "enterprise"}

    @patch("dfe_engine.auth.audit.logger")
    def test_details_defaults_to_none(self, mock_logger):
        audit_org_change(admin_id="admin", org_name="acme", change="deleted")
        _, kwargs = mock_logger.info.call_args
        assert kwargs["details"] is None


class TestAuditOrgChProvisioned:
    @patch("dfe_engine.auth.audit.logger")
    def test_emits_info_event(self, mock_logger):
        audit_org_ch_provisioned(org_name="acme", ch_user="acme_ro", databases=["acme_db"])
        mock_logger.info.assert_called_once()

    @patch("dfe_engine.auth.audit.logger")
    def test_event_name(self, mock_logger):
        audit_org_ch_provisioned(org_name="acme", ch_user="acme_ro", databases=["acme_db"])
        args, _ = mock_logger.info.call_args
        assert args[0] == "org.ch.user_created"

    @patch("dfe_engine.auth.audit.logger")
    def test_structured_fields(self, mock_logger):
        audit_org_ch_provisioned(
            org_name="acme", ch_user="acme_ro", databases=["acme_db", "shared"]
        )
        _, kwargs = mock_logger.info.call_args
        assert kwargs["org_name"] == "acme"
        assert kwargs["ch_user"] == "acme_ro"
        assert kwargs["databases"] == ["acme_db", "shared"]


class TestAuditOrgChFailed:
    @patch("dfe_engine.auth.audit.logger")
    def test_emits_warning_event(self, mock_logger):
        audit_org_ch_failed(org_name="acme", error="connection refused")
        mock_logger.warning.assert_called_once()
        mock_logger.info.assert_not_called()

    @patch("dfe_engine.auth.audit.logger")
    def test_event_name(self, mock_logger):
        audit_org_ch_failed(org_name="acme", error="connection refused")
        args, _ = mock_logger.warning.call_args
        assert args[0] == "org.ch.provision_failed"

    @patch("dfe_engine.auth.audit.logger")
    def test_structured_fields(self, mock_logger):
        audit_org_ch_failed(org_name="acme", error="timeout")
        _, kwargs = mock_logger.warning.call_args
        assert kwargs["org_name"] == "acme"
        assert kwargs["error"] == "timeout"


class TestAuditOrgHyperdxProvisioned:
    @patch("dfe_engine.auth.audit.logger")
    def test_emits_info_event(self, mock_logger):
        audit_org_hyperdx_provisioned(org_name="acme", team_id="team-123")
        mock_logger.info.assert_called_once()

    @patch("dfe_engine.auth.audit.logger")
    def test_event_name(self, mock_logger):
        audit_org_hyperdx_provisioned(org_name="acme", team_id="team-123")
        args, _ = mock_logger.info.call_args
        assert args[0] == "org.hyperdx.team_created"

    @patch("dfe_engine.auth.audit.logger")
    def test_structured_fields(self, mock_logger):
        audit_org_hyperdx_provisioned(org_name="acme", team_id="team-abc")
        _, kwargs = mock_logger.info.call_args
        assert kwargs["org_name"] == "acme"
        assert kwargs["team_id"] == "team-abc"


class TestAuditOrgHyperdxFailed:
    @patch("dfe_engine.auth.audit.logger")
    def test_emits_warning_event(self, mock_logger):
        audit_org_hyperdx_failed(org_name="acme", error="API error")
        mock_logger.warning.assert_called_once()
        mock_logger.info.assert_not_called()

    @patch("dfe_engine.auth.audit.logger")
    def test_event_name(self, mock_logger):
        audit_org_hyperdx_failed(org_name="acme", error="API error")
        args, _ = mock_logger.warning.call_args
        assert args[0] == "org.hyperdx.provision_failed"

    @patch("dfe_engine.auth.audit.logger")
    def test_structured_fields(self, mock_logger):
        audit_org_hyperdx_failed(org_name="acme", error="403 Forbidden")
        _, kwargs = mock_logger.warning.call_args
        assert kwargs["org_name"] == "acme"
        assert kwargs["error"] == "403 Forbidden"


class TestAuditJitAccountCreated:
    @patch("dfe_engine.auth.audit.logger")
    def test_emits_info_event(self, mock_logger):
        audit_jit_account_created(
            user_id="alice@example.com",
            source_provider="entra",
            groups=["soc-analysts"],
            org_ids=["acme"],
        )
        mock_logger.info.assert_called_once()

    @patch("dfe_engine.auth.audit.logger")
    def test_event_name(self, mock_logger):
        audit_jit_account_created(
            user_id="alice@example.com",
            source_provider="entra",
            groups=[],
            org_ids=[],
        )
        args, _ = mock_logger.info.call_args
        assert args[0] == "auth.jit.account_created"

    @patch("dfe_engine.auth.audit.logger")
    def test_structured_fields(self, mock_logger):
        audit_jit_account_created(
            user_id="bob@example.com",
            source_provider="okta",
            groups=["threat-hunters", "soc-analysts"],
            org_ids=["acme", "beta"],
        )
        _, kwargs = mock_logger.info.call_args
        assert kwargs["user_id"] == "bob@example.com"
        assert kwargs["source_provider"] == "okta"
        assert kwargs["groups"] == ["threat-hunters", "soc-analysts"]
        assert kwargs["org_ids"] == ["acme", "beta"]

    @patch("dfe_engine.auth.audit.logger")
    def test_empty_groups_and_orgs(self, mock_logger):
        audit_jit_account_created(
            user_id="new@example.com",
            source_provider="generic",
            groups=[],
            org_ids=[],
        )
        _, kwargs = mock_logger.info.call_args
        assert kwargs["groups"] == []
        assert kwargs["org_ids"] == []


class TestAuditJitGroupsUpdated:
    @patch("dfe_engine.auth.audit.logger")
    def test_emits_info_event(self, mock_logger):
        audit_jit_groups_updated(user_id="alice", added=["admin"], removed=[])
        mock_logger.info.assert_called_once()

    @patch("dfe_engine.auth.audit.logger")
    def test_event_name(self, mock_logger):
        audit_jit_groups_updated(user_id="alice", added=["admin"], removed=[])
        args, _ = mock_logger.info.call_args
        assert args[0] == "auth.jit.groups_updated"

    @patch("dfe_engine.auth.audit.logger")
    def test_structured_fields(self, mock_logger):
        audit_jit_groups_updated(user_id="alice", added=["admin"], removed=["viewer"])
        _, kwargs = mock_logger.info.call_args
        assert kwargs["user_id"] == "alice"
        assert kwargs["added_groups"] == ["admin"]
        assert kwargs["removed_groups"] == ["viewer"]


class TestAuditJitTeamAssigned:
    @patch("dfe_engine.auth.audit.logger")
    def test_emits_info_event(self, mock_logger):
        audit_jit_team_assigned(user_id="alice", team_name="soc", reason="group-mapping")
        mock_logger.info.assert_called_once()

    @patch("dfe_engine.auth.audit.logger")
    def test_event_name(self, mock_logger):
        audit_jit_team_assigned(user_id="alice", team_name="soc", reason="group-mapping")
        args, _ = mock_logger.info.call_args
        assert args[0] == "auth.jit.team_assigned"

    @patch("dfe_engine.auth.audit.logger")
    def test_structured_fields(self, mock_logger):
        audit_jit_team_assigned(
            user_id="bob", team_name="threat-hunters", reason="rule:hunters-map"
        )
        _, kwargs = mock_logger.info.call_args
        assert kwargs["user_id"] == "bob"
        assert kwargs["team_name"] == "threat-hunters"
        assert kwargs["reason"] == "rule:hunters-map"


class TestAuditJitFailed:
    @patch("dfe_engine.auth.audit.logger")
    def test_emits_warning_event(self, mock_logger):
        audit_jit_failed(user_id="alice", error="DB write failed")
        mock_logger.warning.assert_called_once()
        mock_logger.info.assert_not_called()

    @patch("dfe_engine.auth.audit.logger")
    def test_event_name(self, mock_logger):
        audit_jit_failed(user_id="alice", error="DB write failed")
        args, _ = mock_logger.warning.call_args
        assert args[0] == "auth.jit.provision_failed"

    @patch("dfe_engine.auth.audit.logger")
    def test_structured_fields(self, mock_logger):
        audit_jit_failed(user_id="charlie", error="timeout after 5s")
        _, kwargs = mock_logger.warning.call_args
        assert kwargs["user_id"] == "charlie"
        assert kwargs["error"] == "timeout after 5s"


class TestAuditResourceChange:
    @pytest.mark.parametrize("change", ["created", "updated", "deleted"])
    @patch("dfe_engine.auth.audit.logger")
    def test_emits_info_event(self, mock_logger, change):
        audit_resource_change(
            admin_id="admin",
            resource_type="source",
            resource_name="my-source",
            change=change,
        )
        mock_logger.info.assert_called_once()

    @pytest.mark.parametrize(
        ("resource_type", "change"),
        [("source", "created"), ("fieldmap", "updated"), ("rule", "deleted")],
    )
    @patch("dfe_engine.auth.audit.logger")
    def test_event_name_includes_type_and_change(self, mock_logger, resource_type, change):
        audit_resource_change(
            admin_id="admin",
            resource_type=resource_type,
            resource_name="x",
            change=change,
        )
        args, _ = mock_logger.info.call_args
        assert args[0] == f"resource.{resource_type}.{change}"

    @patch("dfe_engine.auth.audit.logger")
    def test_structured_fields(self, mock_logger):
        audit_resource_change(
            admin_id="superadmin",
            resource_type="source",
            resource_name="prod-kafka",
            change="created",
            details={"type": "kafka"},
        )
        _, kwargs = mock_logger.info.call_args
        assert kwargs["admin_id"] == "superadmin"
        assert kwargs["resource_type"] == "source"
        assert kwargs["resource_name"] == "prod-kafka"
        assert kwargs["change"] == "created"
        assert kwargs["details"] == {"type": "kafka"}

    @patch("dfe_engine.auth.audit.logger")
    def test_details_defaults_to_none(self, mock_logger):
        audit_resource_change(
            admin_id="admin",
            resource_type="fieldmap",
            resource_name="my-map",
            change="deleted",
        )
        _, kwargs = mock_logger.info.call_args
        assert kwargs["details"] is None
