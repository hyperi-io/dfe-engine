"""Tests for DFE settings, particularly DFE_CONFIG_DIR resolution."""

import os

import pytest

from dfe_engine.settings import DFESettings, load_settings, reset_settings


@pytest.fixture(autouse=True)
def _clean_settings():
    """Reset global settings after each test."""
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def config_dir(tmp_path):
    """Create a temporary config directory with expected subdirs."""
    for subdir in ("services", "sources", "deployment", "hunts", "hunt-rules", "queries"):
        (tmp_path / subdir).mkdir()
    return str(tmp_path)


class TestConfigDir:
    def test_config_dir_resolves_services_dir(self, config_dir, monkeypatch):
        monkeypatch.setenv("DFE_CONFIG_DIR", config_dir)
        settings = load_settings()
        assert settings.services.config_yaml_dir == os.path.join(config_dir, "services")

    def test_config_dir_resolves_sources_dir(self, config_dir, monkeypatch):
        monkeypatch.setenv("DFE_CONFIG_DIR", config_dir)
        settings = load_settings()
        assert settings.source.sources_dir == os.path.join(config_dir, "sources")

    def test_config_dir_resolves_deployment_dir(self, config_dir, monkeypatch):
        monkeypatch.setenv("DFE_CONFIG_DIR", config_dir)
        settings = load_settings()
        assert settings.deployment.config_dir == os.path.join(config_dir, "deployment")

    def test_config_dir_resolves_hunts_dir(self, config_dir, monkeypatch):
        monkeypatch.setenv("DFE_CONFIG_DIR", config_dir)
        settings = load_settings()
        assert settings.hunts.hunt_dir == os.path.join(config_dir, "hunts")

    def test_config_dir_resolves_hunt_rules_dir(self, config_dir, monkeypatch):
        monkeypatch.setenv("DFE_CONFIG_DIR", config_dir)
        settings = load_settings()
        assert settings.hunts.rule_repo_dir == os.path.join(config_dir, "hunt-rules")

    def test_config_dir_resolves_query_dir(self, config_dir, monkeypatch):
        monkeypatch.setenv("DFE_CONFIG_DIR", config_dir)
        settings = load_settings()
        assert settings.query.yaml_dir == os.path.join(config_dir, "queries")

    def test_config_dir_stored_on_settings(self, config_dir, monkeypatch):
        monkeypatch.setenv("DFE_CONFIG_DIR", config_dir)
        settings = load_settings()
        assert settings.config_dir == config_dir

    def test_specific_var_overrides_config_dir(self, config_dir, monkeypatch, tmp_path):
        """Individual env vars take precedence over DFE_CONFIG_DIR subdirectories."""
        custom_sources = str(tmp_path / "custom-sources")
        monkeypatch.setenv("DFE_CONFIG_DIR", config_dir)
        monkeypatch.setenv("DFE_SOURCES_DIR", custom_sources)
        settings = load_settings()
        # sources_dir should use the specific override
        assert settings.source.sources_dir == custom_sources
        # other dirs should still resolve from config_dir
        assert settings.services.config_yaml_dir == os.path.join(config_dir, "services")

    def test_no_config_dir_leaves_defaults(self, monkeypatch):
        """Without DFE_CONFIG_DIR, registry dirs use their defaults (empty string)."""
        monkeypatch.delenv("DFE_CONFIG_DIR", raising=False)
        settings = load_settings()
        assert settings.config_dir == ""
        assert settings.source.sources_dir == ""
        assert settings.services.config_yaml_dir == ""


class TestDefaultSettings:
    def test_load_defaults(self):
        settings = load_settings()
        assert isinstance(settings, DFESettings)
        assert settings.clickhouse.host == "localhost"
