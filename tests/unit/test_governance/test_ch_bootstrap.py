"""The tenant-isolation switch, and the service users' provided and stored passwords."""

from types import SimpleNamespace

import pytest

from dfe_engine.governance.ch.bootstrap import (
    LEGACY_TENANT_ISOLATION_ENV,
    TENANT_ISOLATION_ENV,
    provided_service_passwords,
    service_user_password,
    tenant_isolation_enabled,
)
from dfe_engine.governance.ch.models import DEFAULT_SERVICE_ROLES
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import SecretsSettings, provided_service_password


def _password_env(role: str) -> str:
    return f"DFE_CLICKHOUSE_{role.upper()}_PASSWORD"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(TENANT_ISOLATION_ENV, raising=False)
    monkeypatch.delenv(LEGACY_TENANT_ISOLATION_ENV, raising=False)
    for role in DEFAULT_SERVICE_ROLES:
        monkeypatch.delenv(_password_env(role.name), raising=False)


class TestDefault:
    def test_unset_is_enabled(self):
        """The row policies are the isolation, so an unconfigured deploy enforces."""
        assert tenant_isolation_enabled() is True

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", " Yes "])
    def test_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv(TENANT_ISOLATION_ENV, value)
        assert tenant_isolation_enabled() is True

    @pytest.mark.parametrize("value", ["false", "FALSE", "0", "no"])
    def test_falsy_values(self, monkeypatch, value):
        monkeypatch.setenv(TENANT_ISOLATION_ENV, value)
        assert tenant_isolation_enabled() is False

    def test_unrecognised_value_is_off(self, monkeypatch):
        """An explicit setting that is not truthy stays off rather than defaulting on."""
        monkeypatch.setenv(TENANT_ISOLATION_ENV, "maybe")
        assert tenant_isolation_enabled() is False


class TestLegacyName:
    def test_legacy_off_is_honoured(self, monkeypatch):
        monkeypatch.setenv(LEGACY_TENANT_ISOLATION_ENV, "false")
        assert tenant_isolation_enabled() is False

    def test_legacy_on_is_honoured(self, monkeypatch):
        monkeypatch.setenv(LEGACY_TENANT_ISOLATION_ENV, "true")
        assert tenant_isolation_enabled() is True

    def test_current_name_wins_over_legacy(self, monkeypatch):
        monkeypatch.setenv(TENANT_ISOLATION_ENV, "true")
        monkeypatch.setenv(LEGACY_TENANT_ISOLATION_ENV, "false")
        assert tenant_isolation_enabled() is True

    def test_empty_legacy_falls_through_to_the_default(self, monkeypatch):
        monkeypatch.setenv(LEGACY_TENANT_ISOLATION_ENV, "")
        assert tenant_isolation_enabled() is True


class TestProvidedPasswords:
    def test_the_env_names_the_role(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD", "given")
        assert provided_service_password("hunt_runner") == "given"

    def test_unset_and_empty_both_mean_none(self, monkeypatch):
        assert provided_service_password("hunt_runner") == ""
        monkeypatch.setenv("DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD", "")
        assert provided_service_password("hunt_runner") == ""

    def test_the_value_is_not_trimmed(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD", " padded ")
        assert provided_service_password("hunt_runner") == " padded "

    def test_only_roles_with_a_value_are_collected(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD", "runner-pw")
        assert provided_service_passwords() == {"hunt_runner": "runner-pw"}

    def test_the_admin_password_is_not_a_service_password(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_PASSWORD", "admin-pw")
        assert provided_service_passwords() == {}


class TestServiceUserPassword:
    def _settings(self, tmp_path):
        return SimpleNamespace(secrets=SecretsSettings(provider="file", path=str(tmp_path)))

    def test_the_stored_secret_is_read_for_a_minted_user(self, tmp_path):
        settings = self._settings(tmp_path)
        build_secrets(settings.secrets).put("ch/service/query_reader", "stored-pw")
        assert service_user_password(settings, "dfe_query_reader") == "stored-pw"

    def test_the_provided_password_wins_over_the_store(self, tmp_path, monkeypatch):
        settings = self._settings(tmp_path)
        build_secrets(settings.secrets).put("ch/service/query_reader", "stored-pw")
        monkeypatch.setenv("DFE_CLICKHOUSE_QUERY_READER_PASSWORD", "provided-pw")
        assert service_user_password(settings, "dfe_query_reader") == "provided-pw"

    def test_a_user_with_nothing_stored_has_no_password(self, tmp_path):
        assert service_user_password(self._settings(tmp_path), "dfe_query_reader") == ""

    @pytest.mark.parametrize("username", ["default", "dfe_otel_reader", "dfe_org_acme"])
    def test_a_user_that_is_not_a_minted_service_user_has_none(self, tmp_path, username):
        settings = self._settings(tmp_path)
        build_secrets(settings.secrets).put("ch/service/otel_reader", "should-not-leak")
        assert service_user_password(settings, username) == ""
