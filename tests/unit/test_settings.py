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


def _clear_registry_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip path overrides so tests/conftest .env cannot shadow DFE_CONFIG_DIR."""
    for key in (
        "DFE_SOURCES_DIR",
        "DFE_DEPLOYMENT_CONFIG_DIR",
        "DFE_SERVICES_CONFIG_YAML_DIR",
        "DFE_HUNTS_DIR",
        "DFE_HUNTS_RULE_REPO_DIR",
        "DFE_QUERY_YAML_DIR",
        "DFE_FIELDMAPS_DIR",
    ):
        monkeypatch.delenv(key, raising=False)


class TestConfigDir:
    def test_config_dir_resolves_services_dir(self, config_dir, monkeypatch):
        _clear_registry_path_env(monkeypatch)
        monkeypatch.setenv("DFE_CONFIG_DIR", config_dir)
        settings = load_settings()
        assert settings.services.config_yaml_dir == os.path.join(config_dir, "services")

    def test_config_dir_resolves_sources_dir(self, config_dir, monkeypatch):
        _clear_registry_path_env(monkeypatch)
        monkeypatch.setenv("DFE_CONFIG_DIR", config_dir)
        settings = load_settings()
        assert settings.source.sources_dir == os.path.join(config_dir, "sources")

    def test_config_dir_resolves_deployment_dir(self, config_dir, monkeypatch):
        _clear_registry_path_env(monkeypatch)
        monkeypatch.setenv("DFE_CONFIG_DIR", config_dir)
        settings = load_settings()
        assert settings.deployment.config_dir == os.path.join(config_dir, "deployment")

    def test_config_dir_resolves_hunts_dir(self, config_dir, monkeypatch):
        _clear_registry_path_env(monkeypatch)
        monkeypatch.setenv("DFE_CONFIG_DIR", config_dir)
        settings = load_settings()
        assert settings.hunts.hunt_dir == os.path.join(config_dir, "hunts")

    def test_config_dir_resolves_hunt_rules_dir(self, config_dir, monkeypatch):
        _clear_registry_path_env(monkeypatch)
        monkeypatch.setenv("DFE_CONFIG_DIR", config_dir)
        settings = load_settings()
        assert settings.hunts.rule_repo_dir == os.path.join(config_dir, "hunt-rules")

    def test_config_dir_resolves_query_dir(self, config_dir, monkeypatch):
        _clear_registry_path_env(monkeypatch)
        monkeypatch.setenv("DFE_CONFIG_DIR", config_dir)
        settings = load_settings()
        assert settings.query.yaml_dir == os.path.join(config_dir, "queries")

    def test_config_dir_stored_on_settings(self, config_dir, monkeypatch):
        _clear_registry_path_env(monkeypatch)
        monkeypatch.setenv("DFE_CONFIG_DIR", config_dir)
        settings = load_settings()
        assert settings.config_dir == config_dir

    def test_specific_var_overrides_config_dir(self, config_dir, monkeypatch, tmp_path):
        """Individual env vars take precedence over DFE_CONFIG_DIR subdirectories."""
        custom_sources = str(tmp_path / "custom-sources")
        _clear_registry_path_env(monkeypatch)
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
        _clear_registry_path_env(monkeypatch)
        settings = load_settings()
        assert settings.config_dir == ""
        assert settings.source.sources_dir == ""
        assert settings.services.config_yaml_dir == ""


class TestDefaultSettings:
    def test_load_defaults(self):
        settings = load_settings()
        assert isinstance(settings, DFESettings)
        assert settings.clickhouse.host == "localhost"


class TestEnvOverrides:
    """Test env var overrides for the config cascade in load_settings()."""

    def test_clickhouse_host_override(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_HOST", "ch.example.com")
        settings = load_settings()
        assert settings.clickhouse.host == "ch.example.com"

    def test_clickhouse_port_override(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_PORT", "9000")
        settings = load_settings()
        assert settings.clickhouse.port == 9000

    def test_clickhouse_username_override(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_USERNAME", "admin")
        settings = load_settings()
        assert settings.clickhouse.username == "admin"

    def test_clickhouse_database_override(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_DATABASE", "analytics")
        settings = load_settings()
        assert settings.clickhouse.database == "analytics"

    def test_clickhouse_secure_true(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_SECURE", "true")
        settings = load_settings()
        assert settings.clickhouse.secure is True

    def test_clickhouse_secure_false(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_SECURE", "false")
        settings = load_settings()
        assert settings.clickhouse.secure is False

    def test_clickhouse_connections_min(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_CONNECTIONS_MIN", "5")
        settings = load_settings()
        assert settings.clickhouse.connections_min == 5

    def test_clickhouse_connections_max(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_CONNECTIONS_MAX", "20")
        settings = load_settings()
        assert settings.clickhouse.connections_max == 20

    def test_api_host_override(self, monkeypatch):
        monkeypatch.setenv("DFE_API_HOST", "127.0.0.1")
        settings = load_settings()
        assert settings.api.host == "127.0.0.1"

    def test_api_port_override(self, monkeypatch):
        monkeypatch.setenv("DFE_API_PORT", "9090")
        settings = load_settings()
        assert settings.api.port == 9090

    def test_api_jwt_secret_override(self, monkeypatch):
        monkeypatch.setenv("DFE_API_JWT_SECRET", "super-secret")
        settings = load_settings()
        assert settings.api.jwt_secret == "super-secret"

    def test_api_cors_origins_override(self, monkeypatch):
        monkeypatch.setenv("DFE_API_CORS_ORIGINS", "http://a.com,http://b.com")
        settings = load_settings()
        assert "http://a.com" in settings.api.cors_origins

    def test_api_elastic_converter_max_upload_override(self, monkeypatch):
        monkeypatch.setenv("DFE_API_ELASTIC_CONVERTER_MAX_UPLOAD_BYTES", "1048576")
        settings = load_settings()
        assert settings.api.elastic_converter_max_upload_bytes == 1_048_576

    def test_api_elastic_converter_read_chunk_override(self, monkeypatch):
        monkeypatch.setenv("DFE_API_ELASTIC_CONVERTER_READ_CHUNK_SIZE", "32768")
        settings = load_settings()
        assert settings.api.elastic_converter_read_chunk_size == 32_768

    def test_api_elastic_converter_content_length_slack_override(self, monkeypatch):
        monkeypatch.setenv("DFE_API_ELASTIC_CONVERTER_CONTENT_LENGTH_SLACK_BYTES", "65536")
        settings = load_settings()
        assert settings.api.elastic_converter_content_length_slack_bytes == 65_536

    def test_auth_enabled_override(self, monkeypatch):
        monkeypatch.setenv("DFE_AUTH_ENABLED", "true")
        settings = load_settings()
        assert settings.auth.enabled is True

    def test_auth_dir_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DFE_AUTH_DIR", str(tmp_path / "auth"))
        settings = load_settings()
        assert settings.auth.auth_dir == str(tmp_path / "auth")

    def test_hunt_log_path_override(self, monkeypatch):
        monkeypatch.setenv("DFE_HUNT_LOG_PATH", "/var/log/hunts")
        settings = load_settings()
        assert settings.hunts.log_path == "/var/log/hunts"

    def test_legacy_clickhouse_host_fallback(self, monkeypatch):
        """Legacy env var (no DFE_ prefix) should also work."""
        monkeypatch.setenv("CLICKHOUSE_HOST", "legacy.ch.com")
        settings = load_settings()
        assert settings.clickhouse.host == "legacy.ch.com"

    def test_dfe_prefix_takes_precedence(self, monkeypatch):
        """DFE_ prefixed var takes precedence over legacy var."""
        monkeypatch.setenv("CLICKHOUSE_HOST", "legacy.ch.com")
        monkeypatch.setenv("DFE_CLICKHOUSE_HOST", "new.ch.com")
        settings = load_settings()
        assert settings.clickhouse.host == "new.ch.com"

    def test_hyperdx_enabled_override(self, monkeypatch):
        monkeypatch.setenv("DFE_HYPERDX_ENABLED", "true")
        settings = load_settings()
        assert settings.hyperdx.enabled is True

    def test_hyperdx_base_url_override(self, monkeypatch):
        monkeypatch.setenv("DFE_HYPERDX_BASE_URL", "http://hdx:8080")
        settings = load_settings()
        assert settings.hyperdx.base_url == "http://hdx:8080"

    def test_clickhouse_verify_true(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_VERIFY", "1")
        settings = load_settings()
        assert settings.clickhouse.verify is True

    def test_clickhouse_password_override(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_PASSWORD", "secret123")
        settings = load_settings()
        assert settings.clickhouse.password == "secret123"

    def test_api_jwt_expire_override(self, monkeypatch):
        monkeypatch.setenv("DFE_API_JWT_EXPIRE_MINUTES", "60")
        settings = load_settings()
        assert settings.api.jwt_expire_minutes == 60

    def test_hunts_dir_override(self, monkeypatch):
        monkeypatch.setenv("DFE_HUNTS_DIR", "/custom/hunts")
        settings = load_settings()
        assert settings.hunts.hunt_dir == "/custom/hunts"

    def test_hunt_rules_dir_override(self, monkeypatch):
        monkeypatch.setenv("DFE_HUNTS_RULE_REPO_DIR", "/custom/rules")
        settings = load_settings()
        assert settings.hunts.rule_repo_dir == "/custom/rules"

    def test_query_yaml_dir_override(self, monkeypatch):
        monkeypatch.setenv("DFE_QUERY_YAML_DIR", "/custom/queries")
        settings = load_settings()
        assert settings.query.yaml_dir == "/custom/queries"

    def test_helm_output_dir_override(self, monkeypatch):
        monkeypatch.setenv("DFE_HELM_OUTPUT_DIR", "/helm/out")
        settings = load_settings()
        assert settings.helm.output_dir == "/helm/out"

    def test_helm_environment_file_override(self, monkeypatch):
        monkeypatch.setenv("DFE_HELM_ENVIRONMENT_FILE", "/env.yaml")
        settings = load_settings()
        assert settings.helm.environment_file == "/env.yaml"

    def test_fieldmap_dir_override(self, monkeypatch):
        monkeypatch.setenv("DFE_FIELDMAPS_DIR", "/custom/fieldmaps")
        settings = load_settings()
        assert settings.fieldmap.fieldmaps_dir == "/custom/fieldmaps"

    def test_alert_channels_override(self, monkeypatch):
        monkeypatch.setenv("DFE_HUNTS_ALERT_CHANNELS", "email,slack")
        settings = load_settings()
        assert "email" in settings.hunts.alert_channels

    def test_deployment_config_dir_override(self, monkeypatch):
        monkeypatch.setenv("DFE_DEPLOYMENT_CONFIG_DIR", "/custom/deploy")
        settings = load_settings()
        assert settings.deployment.config_dir == "/custom/deploy"

    def test_services_config_yaml_dir_override(self, monkeypatch):
        monkeypatch.setenv("DFE_SERVICES_CONFIG_YAML_DIR", "/custom/svc")
        settings = load_settings()
        assert settings.services.config_yaml_dir == "/custom/svc"

    def test_sources_dir_override(self, monkeypatch):
        monkeypatch.setenv("DFE_SOURCES_DIR", "/custom/sources")
        settings = load_settings()
        assert settings.source.sources_dir == "/custom/sources"

    def test_hunt_default_cooldown_override(self, monkeypatch):
        monkeypatch.setenv("DFE_HUNTS_DEFAULT_ALERT_COOLDOWN", "600")
        settings = load_settings()
        assert settings.hunts.default_alert_cooldown == "600"

    def test_hunt_max_alerts_per_run_override(self, monkeypatch):
        monkeypatch.setenv("DFE_HUNTS_DEFAULT_MAX_ALERTS_PER_RUN", "50")
        settings = load_settings()
        assert settings.hunts.default_max_alerts_per_run == 50

    def test_hyperdx_api_key_env_override(self, monkeypatch):
        monkeypatch.setenv("DFE_HYPERDX_API_KEY_ENV", "MY_KEY_VAR")
        settings = load_settings()
        assert settings.hyperdx.api_key_env == "MY_KEY_VAR"

    def test_hunt_alert_destinations_json_override(self, monkeypatch):
        monkeypatch.setenv("DFE_HUNTS_ALERT_DESTINATIONS", '{"email": "alert@test.com"}')
        settings = load_settings()
        assert settings.hunts.alert_destinations == {"email": "alert@test.com"}

    def test_multiple_overrides_at_once(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_HOST", "ch1.example.com")
        monkeypatch.setenv("DFE_CLICKHOUSE_PORT", "9000")
        monkeypatch.setenv("DFE_API_PORT", "9090")
        monkeypatch.setenv("DFE_AUTH_ENABLED", "true")
        settings = load_settings()
        assert settings.clickhouse.host == "ch1.example.com"
        assert settings.clickhouse.port == 9000
        assert settings.api.port == 9090
        assert settings.auth.enabled is True
