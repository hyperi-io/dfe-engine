#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_credential_env.py
#  Purpose:      Tests for OIDC credential env var helpers
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from dfe_engine.auth.oidc.credential_env import credential_env_check


class TestCredentialEnvCheck:
    def test_missing_env_name(self):
        ok, detail = credential_env_check("client_id", "")
        assert ok is False
        assert "no env var" in detail

    def test_resolved_env(self, monkeypatch):
        monkeypatch.setenv("MY_OIDC_CLIENT_ID", "secret-value")
        ok, detail = credential_env_check("client_id", "MY_OIDC_CLIENT_ID")
        assert ok is True
        assert "secret-value" not in detail

    def test_unset_valid_env_name(self, monkeypatch):
        monkeypatch.delenv("UNSET_OIDC_ID", raising=False)
        ok, detail = credential_env_check("client_id", "UNSET_OIDC_ID")
        assert ok is False
        assert "unset or empty" in detail

    def test_literal_okta_client_id_rejected(self):
        ok, detail = credential_env_check("client_id", "0oa15mxzztuHEzwr7698")
        assert ok is False
        assert "environment variable name" in detail
