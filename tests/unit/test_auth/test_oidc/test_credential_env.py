#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_credential_env.py
#  Purpose:      Tests for OIDC credential resolution (store before env)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dfe_engine.auth.oidc.credential_env import (
    credential_check,
    env_var_resolves,
    is_env_var_name,
    new_secret_version,
    provider_secret_path,
    resolve_credential,
)
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import SecretsSettings
from tests.unit.test_auth.test_oidc.refusing_store import (
    AppRoleRefusingSecretStore,
    PolicyRefusingSecretStore,
)
from tests.unit.test_auth.test_oidc.unreachable_store import (
    STORE_ADDRESS,
    UnreachableSecretStore,
)


@pytest.fixture
def store(tmp_path):
    """A real scalo.secrets file backend under tmp_path - no mocks."""
    return build_secrets(SecretsSettings(provider="file", path=str(tmp_path / "secrets")))


class _LoginHandler(BaseHTTPRequestHandler):
    """Answers every request with ``status``, as OpenBao answers an AppRole login."""

    status = 200

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(self.status)
        if 300 <= self.status < 400:
            self.send_header("Location", "/v1/elsewhere")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture(scope="module")
def openbao_login() -> Iterator[str]:
    """A real local HTTP server standing in for OpenBao's login endpoint."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _LoginHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


class TestCredentialCheck:
    def test_missing_env_name(self):
        ok, detail = credential_check("client_id")
        assert ok is False
        assert "no env var" in detail

    def test_resolved_env(self, monkeypatch):
        monkeypatch.setenv("MY_OIDC_CLIENT_ID", "secret-value")
        ok, detail = credential_check("client_id", env_name="MY_OIDC_CLIENT_ID")
        assert ok is True
        assert "secret-value" not in detail

    def test_unset_valid_env_name(self, monkeypatch):
        monkeypatch.delenv("UNSET_OIDC_ID", raising=False)
        ok, detail = credential_check("client_id", env_name="UNSET_OIDC_ID")
        assert ok is False
        assert "unset or empty" in detail

    def test_literal_okta_client_id_rejected(self):
        ok, detail = credential_check("client_id", env_name="0oa15mxzztuHEzwr7698")
        assert ok is False
        assert "environment variable name" in detail
        assert "0oa" not in detail

    def test_plain_value_passes(self):
        ok, detail = credential_check("client_id", value="0oa15mxzztuHEzwr7698")
        assert ok is True
        assert "0oa" not in detail

    def test_stored_secret_passes_and_names_the_path(self, store):
        store.put("oidc/acme/client_secret", "stored-value")
        ok, detail = credential_check(
            "client_secret", secret_path="oidc/acme/client_secret", secrets=store
        )
        assert ok is True
        assert "oidc/acme/client_secret" in detail
        assert "stored-value" not in detail

    def test_whitespace_only_env_is_unset(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("BLANK_OIDC_ID", " \n")
        result = credential_check("client_id", env_name="BLANK_OIDC_ID")
        assert result == (False, "BLANK_OIDC_ID is unset or empty")

    def test_path_with_nothing_stored_and_no_env_is_reported(self, store):
        ok, detail = credential_check(
            "client_secret", secret_path="oidc/acme/client_secret", secrets=store
        )
        assert ok is False
        assert "nothing stored at oidc/acme/client_secret" in detail

    def test_a_store_that_did_not_answer_is_named_and_the_env_still_serves(self, monkeypatch):
        """The check reports the outage; a login falls through to the env var, so the check does too."""
        monkeypatch.delenv("OIDC_RP_SECRET", raising=False)

        ok, detail = credential_check(
            "client_secret",
            secret_path="oidc/acme/client_secret",
            env_name="OIDC_RP_SECRET",
            secrets=UnreachableSecretStore(),
        )
        monkeypatch.setenv("OIDC_RP_SECRET", "from-env")
        served, _detail = credential_check(
            "client_secret",
            secret_path="oidc/acme/client_secret",
            env_name="OIDC_RP_SECRET",
            secrets=UnreachableSecretStore(),
        )

        assert ok is False
        assert "secret store did not answer" in detail
        assert "oidc/acme/client_secret" in detail
        assert "unset or empty" not in detail
        assert served is True

    def test_a_store_that_did_not_answer_with_no_env_var_is_named(self):
        ok, detail = credential_check(
            "client_secret", secret_path="oidc/acme/client_secret", secrets=UnreachableSecretStore()
        )

        assert ok is False
        assert "secret store did not answer" in detail
        assert "nothing stored" not in detail

    @pytest.mark.parametrize(
        "store",
        [PolicyRefusingSecretStore(), AppRoleRefusingSecretStore()],
        ids=["policy-403", "approle-login"],
    )
    def test_a_store_that_refused_points_at_its_policy_not_its_reachability(self, store):
        ok, detail = credential_check(
            "client_secret", secret_path="oidc/acme/client_secret", secrets=store
        )

        assert ok is False
        assert detail == (
            "The secret store refused access to the client_secret at oidc/acme/client_secret, "
            "so check the engine's secrets policy; the engine log has the reason"
        )


class TestResolveCredential:
    def test_store_wins_over_env(self, store, monkeypatch):
        """A secret written through the API beats a stale env var of the same name."""
        monkeypatch.setenv("OIDC_RP_SECRET", "from-env")
        store.put("oidc/acme/client_secret", "from-store")
        resolved = resolve_credential(
            secret_path="oidc/acme/client_secret",
            env_name="OIDC_RP_SECRET",
            secrets=store,
        )
        assert resolved.value == "from-store"

    def test_env_serves_a_provider_with_no_stored_secret(self, store, monkeypatch):
        """An ESO-mounted env var keeps working when the store holds nothing."""
        monkeypatch.setenv("OIDC_RP_SECRET", "from-env")
        resolved = resolve_credential(
            secret_path="oidc/acme/client_secret",
            env_name="OIDC_RP_SECRET",
            secrets=store,
        )
        assert resolved.value == "from-env"

    def test_env_serves_when_no_store_is_wired(self, monkeypatch):
        monkeypatch.setenv("OIDC_RP_SECRET", "from-env")
        assert resolve_credential(env_name="OIDC_RP_SECRET").value == "from-env"

    def test_plain_value_wins_over_both(self, store, monkeypatch):
        monkeypatch.setenv("OIDC_CLIENT_ID", "from-env")
        store.put("oidc/acme/client_secret", "from-store")
        resolved = resolve_credential(
            value="plain-id",
            secret_path="oidc/acme/client_secret",
            env_name="OIDC_CLIENT_ID",
            secrets=store,
        )
        assert resolved.value == "plain-id"

    def test_nothing_configured_resolves_empty(self, store):
        assert resolve_credential(secrets=store).value == ""

    def test_unreachable_store_falls_through_to_env(self, monkeypatch):
        """A store that raises must not take the login path down with it."""
        monkeypatch.setenv("OIDC_RP_SECRET", "from-env")
        resolved = resolve_credential(
            secret_path="oidc/acme/client_secret",
            env_name="OIDC_RP_SECRET",
            secrets=UnreachableSecretStore(),
        )
        assert (resolved.value, resolved.unreadable) == ("from-env", False)

    def test_a_store_that_did_not_answer_is_unreadable_not_absent(self, monkeypatch):
        """With nothing in the env, a store outage is not a credential nobody configured."""
        monkeypatch.delenv("OIDC_RP_SECRET", raising=False)
        resolved = resolve_credential(
            secret_path="oidc/acme/client_secret",
            env_name="OIDC_RP_SECRET",
            secrets=UnreachableSecretStore(),
        )
        assert (resolved.value, resolved.unreadable) == ("", True)

    def test_a_store_holding_nothing_is_absent_not_unreadable(self, store):
        resolved = resolve_credential(secret_path="oidc/acme/client_secret", secrets=store)
        assert (resolved.value, resolved.unreadable) == ("", False)

    @pytest.mark.parametrize(
        ("store", "refused"),
        [
            (PolicyRefusingSecretStore(), True),
            (AppRoleRefusingSecretStore(), True),
            (UnreachableSecretStore(), False),
        ],
        ids=["policy-403", "approle-refused", "unreachable"],
    )
    def test_a_refusal_is_told_apart_from_a_store_that_did_not_answer(self, store, refused):
        resolved = resolve_credential(secret_path="oidc/acme/client_secret", secrets=store)
        assert (resolved.value, resolved.unreadable, resolved.refused) == ("", True, refused)

    @pytest.mark.parametrize(
        ("status", "refused"),
        [
            pytest.param(400, True, id="400-refused"),
            pytest.param(403, True, id="403-refused"),
            pytest.param(429, False, id="429-rate-limited"),
            pytest.param(307, False, id="307-redirect"),
            pytest.param(503, False, id="503-sealed"),
        ],
    )
    def test_an_approle_login_is_classified_by_its_answer(
        self, monkeypatch, openbao_login, status, refused
    ):
        """scalo's own OpenBao provider classifies a real HTTP answer to the AppRole login."""
        monkeypatch.setenv("VAULT_SECRET_ID", "approle-secret-id")
        _LoginHandler.status = status
        store = build_secrets(SecretsSettings(provider="openbao", addr=openbao_login, role="dfe"))

        resolved = resolve_credential(secret_path="oidc/acme/client_secret", secrets=store)

        assert (resolved.value, resolved.unreadable, resolved.refused) == ("", True, refused)

    def test_an_approle_login_never_sent_is_not_a_refusal(self, monkeypatch, openbao_login):
        """No secret id means scalo raises before any request, though this store would refuse."""
        monkeypatch.delenv("VAULT_SECRET_ID", raising=False)
        _LoginHandler.status = 403
        store = build_secrets(SecretsSettings(provider="openbao", addr=openbao_login, role="dfe"))

        resolved = resolve_credential(secret_path="oidc/acme/client_secret", secrets=store)

        assert (resolved.value, resolved.unreadable, resolved.refused) == ("", True, False)

    def test_an_approle_login_with_no_listener_is_not_a_refusal(self, monkeypatch):
        monkeypatch.setenv("VAULT_SECRET_ID", "approle-secret-id")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        store = build_secrets(
            SecretsSettings(provider="openbao", addr=f"http://127.0.0.1:{port}", role="dfe")
        )

        resolved = resolve_credential(secret_path="oidc/acme/client_secret", secrets=store)

        assert (resolved.value, resolved.unreadable, resolved.refused) == ("", True, False)

    def test_the_env_still_serves_when_the_store_refused(self, monkeypatch):
        monkeypatch.setenv("OIDC_RP_SECRET", "from-env")
        resolved = resolve_credential(
            secret_path="oidc/acme/client_secret",
            env_name="OIDC_RP_SECRET",
            secrets=PolicyRefusingSecretStore(),
        )
        assert (resolved.value, resolved.unreadable, resolved.refused) == ("from-env", False, False)

    def test_the_repr_never_carries_the_value(self, store):
        store.put("oidc/acme/client_secret", "stored-value")
        resolved = resolve_credential(secret_path="oidc/acme/client_secret", secrets=store)
        assert "stored-value" not in repr(resolved)

    def test_a_store_that_did_not_answer_is_logged_by_class_not_text(self, audit_events):
        resolve_credential(secret_path="oidc/acme/client_secret", secrets=UnreachableSecretStore())

        (line,) = [
            e
            for e in audit_events
            if e["event"] == "OIDC credential unreadable from the secret store"
        ]
        assert STORE_ADDRESS not in json.dumps(audit_events, default=str)
        assert "error" not in line
        assert (line["error_type"], line["transport_failure"], line["store_path"]) == (
            "ConnectionError",
            "connection_refused",
            "oidc/acme/client_secret",
        )

    def test_surrounding_whitespace_is_stripped_from_the_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("OIDC_RP_SECRET", " from-env\n")
        assert resolve_credential(env_name="OIDC_RP_SECRET").value == "from-env"

    def test_surrounding_whitespace_is_stripped_from_the_store(self, store):
        store.put("oidc/acme/client_secret", "from-store\n")
        resolved = resolve_credential(secret_path="oidc/acme/client_secret", secrets=store)
        assert resolved.value == "from-store"

    def test_surrounding_whitespace_is_stripped_from_a_value(self):
        assert resolve_credential(value=" plain-id ").value == "plain-id"

    def test_a_whitespace_only_value_falls_through_to_the_env(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("OIDC_CLIENT_ID", "from-env")
        assert resolve_credential(env_name="OIDC_CLIENT_ID", value=" ").value == "from-env"


class TestPathAndNameHelpers:
    def test_provider_secret_path_shape(self):
        path = provider_secret_path("acme", "client_secret", "0123456789abcdef")

        assert path == "oidc/acme/0123456789abcdef/client_secret"

    def test_every_secret_version_is_new(self):
        versions = {new_secret_version() for _ in range(1000)}

        assert len(versions) == 1000
        assert all(len(version) == 16 for version in versions)

    @pytest.mark.parametrize("name", ["OKTA_CLIENT_ID", "_private", "A1_b2"])
    def test_env_var_names_accepted(self, name):
        assert is_env_var_name(name) is True

    @pytest.mark.parametrize(
        "name", ["0oa15mxzztuHEzwr7698", "has-dash", "has space", "", "OKTA_CLIENT_ID\n"]
    )
    def test_non_env_var_names_rejected(self, name):
        assert is_env_var_name(name) is False


class TestEnvVarResolves:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("from-env", True, id="set"),
            pytest.param("  from-env\n", True, id="set_with_surrounding_whitespace"),
            pytest.param(" \n", False, id="whitespace_only"),
            pytest.param("", False, id="empty"),
        ],
    )
    def test_matches_expected(self, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool):
        monkeypatch.setenv("DFE_TEST_RESOLVES", value)
        assert env_var_resolves(env_name="DFE_TEST_RESOLVES") is expected

    def test_an_unset_one_does_not(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("DFE_TEST_RESOLVES", raising=False)
        assert env_var_resolves(env_name="DFE_TEST_RESOLVES") is False
