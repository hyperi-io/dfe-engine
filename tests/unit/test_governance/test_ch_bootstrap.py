"""The tenant-isolation switch: on by default, with the retired name still read."""

import pytest

from dfe_engine.governance.ch.bootstrap import (
    LEGACY_TENANT_ISOLATION_ENV,
    TENANT_ISOLATION_ENV,
    tenant_isolation_enabled,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(TENANT_ISOLATION_ENV, raising=False)
    monkeypatch.delenv(LEGACY_TENANT_ISOLATION_ENV, raising=False)


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
