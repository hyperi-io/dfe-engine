#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_audit.py
#  Purpose:      Tests that audit functions emit correct structured log events.
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for SOC2 audit event emitters.

Mocking the logger IS acceptable here — we are testing that the correct log
function is called with the correct structured arguments. This is one of the
explicit exceptions to the no-mocks policy for audit logging.
"""

from unittest.mock import call, patch

import pytest

from dfe_engine.auth.audit import (
    audit_account_change,
    audit_api_key_change,
    audit_group_change,
    audit_login_denied,
    audit_login_success,
    audit_permission_denied,
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
