"""Tests for DFE settings, particularly DFE_CONFIG_DIR resolution."""

import os

import pytest
from pydantic import ValidationError

from dfe_engine.settings import AuthSettings, DFESettings, load_settings, reset_settings


@pytest.fixture(autouse=True)
def _clean_settings():
    """Reset global settings after each test."""
    reset_settings()
    yield
    reset_settings()


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch, tmp_path):
    """Isolate settings tests from a populated developer .env.

    tests/conftest.py loads the project .env with override=True, so a real .env
    (CH host, DFE_ENV=dev, a jwt secret, ...) leaks into os.environ and breaks the
    default / fallback / production-guard assertions here. Strip every DFE_* and
    legacy CLICKHOUSE_* var so each test controls exactly the environment it sets.

    Scrubbing os.environ alone is not enough: ``load_settings()`` calls
    ``load_env_files()``, which re-reads ``./.env`` from the cwd on every call
    and repopulates what was just deleted -- so also run from an empty dir.

    The posture goes back afterwards. With nothing set, the shipped defaults are
    env "production" and auth on with the placeholder jwt_secret, which
    ``DFESettings`` rejects by design -- correct for a deployment, useless for the
    forty-odd tests here that call ``load_settings()`` to check an unrelated field
    mapping. Tests that DO exercise the posture set DFE_ENV themselves or build
    ``DFESettings`` directly, and both override this.
    """
    for key in list(os.environ):
        if key.startswith(("DFE_", "CLICKHOUSE_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DFE_ENV", "test")
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def config_dir(tmp_path):
    """Create a temporary config directory with expected subdirs."""
    for subdir in ("services", "sources", "deployment", "hunts", "hunt-rules", "rules", "queries"):
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

    def test_config_dir_resolves_api_rules_dir(self, config_dir, monkeypatch):
        _clear_registry_path_env(monkeypatch)
        monkeypatch.setenv("DFE_CONFIG_DIR", config_dir)
        settings = load_settings()
        assert settings.hunts.rules_dir == os.path.join(config_dir, "rules")

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

    def test_clickhouse_data_database_override(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_DATABASE", "default")
        monkeypatch.setenv("DFE_CLICKHOUSE_DATA_DATABASE", "dfe")
        settings = load_settings()
        assert settings.clickhouse.data_database == "dfe"
        assert settings.clickhouse.effective_data_database == "dfe"

    def test_one_database_holds_everything(self, monkeypatch):
        """There is ONE database setting, and telemetry is not a second one.

        The otel, hunts and internal databases were separate knobs naming the
        same place; a rename had to be applied to each or the grants and the
        tables disagreed.
        """
        monkeypatch.delenv("DFE_CLICKHOUSE_DATA_DATABASE", raising=False)
        clickhouse = load_settings().clickhouse
        assert clickhouse.effective_data_database == "dfe"
        for gone in ("otel_database", "hunts_database", "internal_database"):
            assert not hasattr(clickhouse, gone)

    def test_renaming_the_database_moves_everything(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_DATA_DATABASE", "telemetry")
        assert load_settings().clickhouse.effective_data_database == "telemetry"

    def test_effective_data_database_defaults_to_dfe(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_DATABASE", "analytics")
        monkeypatch.delenv("DFE_CLICKHOUSE_DATA_DATABASE", raising=False)
        settings = load_settings()
        assert settings.clickhouse.data_database == "dfe"
        assert settings.clickhouse.effective_data_database == "dfe"

    def test_effective_data_database_falls_back_when_emptied(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_DATABASE", "analytics")
        settings = load_settings()
        settings.clickhouse.data_database = ""
        assert settings.clickhouse.effective_data_database == "analytics"

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
        monkeypatch.setenv("DFE_API_JWT_SECRET", "super-secret-hmac-key-at-least-32-bytes")
        settings = load_settings()
        assert settings.api.jwt_secret == "super-secret-hmac-key-at-least-32-bytes"

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
        monkeypatch.setenv("DFE_ENV", "dev")  # dev posture: placeholder secret allowed
        settings = load_settings()
        assert settings.auth.enabled is True

    def test_placeholder_jwt_secret_rejected_in_production(self, monkeypatch):
        # Security guard: auth on + production posture + the known dev secret must
        # fail fast rather than run with a forgeable token key. The posture is set
        # explicitly -- _hermetic_env declares a dev one, which would make this
        # pass or fail for the wrong reason.
        monkeypatch.setenv("DFE_ENV", "production")
        monkeypatch.setenv("DFE_AUTH_ENABLED", "true")
        with pytest.raises(ValidationError):
            load_settings()

    def test_placeholder_jwt_secret_allowed_in_dev(self, monkeypatch):
        monkeypatch.setenv("DFE_AUTH_ENABLED", "true")
        monkeypatch.setenv("DFE_ENV", "dev")
        assert load_settings().auth.enabled is True

    def test_production_accepts_real_jwt_secret(self, monkeypatch):
        monkeypatch.setenv("DFE_ENV", "production")
        monkeypatch.setenv("DFE_AUTH_ENABLED", "true")
        monkeypatch.setenv("DFE_API_JWT_SECRET", "a-real-production-secret-over-32-bytes")
        assert load_settings().auth.enabled is True

    def test_auth_dir_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DFE_AUTH_DIR", str(tmp_path / "auth"))
        settings = load_settings()
        assert settings.auth.auth_dir == str(tmp_path / "auth")

    def test_hunt_log_path_override(self, monkeypatch):
        monkeypatch.setenv("DFE_HUNT_LOG_PATH", "/var/log/hunts")
        settings = load_settings()
        assert settings.hunts.log_path == "/var/log/hunts"

    def test_seed_accounts_json_override(self, monkeypatch):
        monkeypatch.setenv(
            "DFE_AUTH_LOCAL_SEED_ACCOUNTS",
            '[{"username": "kay", "password": "pw-long", "groups": ["dfe-analysts"]}]',
        )
        settings = load_settings()
        assert len(settings.auth.local.seed_accounts) == 1
        seed = settings.auth.local.seed_accounts[0]
        assert seed.username == "kay"
        assert seed.password == "pw-long"
        assert seed.groups == ["dfe-analysts"]

    def test_seed_accounts_malformed_json_fails_loud(self, monkeypatch):
        monkeypatch.setenv("DFE_AUTH_LOCAL_SEED_ACCOUNTS", "{not-json")
        with pytest.raises(ValueError, match="not valid JSON"):
            load_settings()

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

    def test_clickhouse_verify_default_is_none(self):
        # None -> follow the SCALO_TLS_VERIFY escape valve (cert verification ON by
        # default). Replaces the old insecure default of verify=False.
        settings = load_settings()
        assert settings.clickhouse.verify is None

    def test_clickhouse_verify_false(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_VERIFY", "false")
        settings = load_settings()
        assert settings.clickhouse.verify is False

    def test_clickhouse_ca_cert_override_flows_to_config(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_CA_CERT", "/etc/ssl/internal-ca.pem")
        settings = load_settings()
        assert settings.clickhouse.ca_cert == "/etc/ssl/internal-ca.pem"
        from dfe_engine.settings import get_clickhouse_config

        assert get_clickhouse_config(settings)["ch_ca_cert"] == "/etc/ssl/internal-ca.pem"

    def test_dfe_tls_verify_bridges_to_scalo_env(self, monkeypatch):
        # The DFE_-prefixed valve maps onto scalo's env seam so one setting flips
        # every scalo client. monkeypatch.delenv restores SCALO_TLS_VERIFY on teardown.
        from scalo.crypto import tls_verify_default

        monkeypatch.delenv("SCALO_TLS_VERIFY", raising=False)
        monkeypatch.setenv("DFE_TLS_VERIFY", "false")
        load_settings()
        assert os.environ.get("SCALO_TLS_VERIFY") == "false"
        assert tls_verify_default() is False

    def test_dfe_tls_allow_weak_bridges_to_scalo_env(self, monkeypatch):
        from scalo.crypto import tls_allow_weak

        monkeypatch.delenv("SCALO_TLS_ALLOW_WEAK", raising=False)
        monkeypatch.setenv("DFE_TLS_ALLOW_WEAK", "true")
        load_settings()
        assert tls_allow_weak() is True

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

    def test_hunt_alert_destinations_dir_override(self, monkeypatch):
        monkeypatch.setenv("DFE_HUNTS_ALERT_DESTINATIONS_DIR", "/data/alert-destinations")
        settings = load_settings()
        assert settings.hunts.alert_destinations_dir == "/data/alert-destinations"

    def test_multiple_overrides_at_once(self, monkeypatch):
        monkeypatch.setenv("DFE_CLICKHOUSE_HOST", "ch1.example.com")
        monkeypatch.setenv("DFE_CLICKHOUSE_PORT", "9000")
        monkeypatch.setenv("DFE_API_PORT", "9090")
        monkeypatch.setenv("DFE_AUTH_ENABLED", "true")
        monkeypatch.setenv("DFE_ENV", "dev")  # dev posture: placeholder secret allowed
        settings = load_settings()
        assert settings.clickhouse.host == "ch1.example.com"
        assert settings.clickhouse.port == 9000
        assert settings.api.port == 9090
        assert settings.auth.enabled is True


class TestGitopsMode:
    def test_default_is_team(self):
        from dfe_engine.settings import GitopsSettings

        assert GitopsSettings().mode == "team"

    def test_solo_accepted(self):
        from dfe_engine.settings import GitopsSettings

        assert GitopsSettings(mode="solo").mode == "solo"

    def test_invalid_mode_rejected(self):
        import pytest
        from pydantic import ValidationError

        from dfe_engine.settings import GitopsSettings

        with pytest.raises(ValidationError):
            GitopsSettings(mode="duo")

    def test_env_override(self, monkeypatch):
        from dfe_engine.settings import _get_env_overrides

        monkeypatch.setenv("DFE_GITOPS_MODE", "solo")
        overrides = _get_env_overrides()
        assert overrides["gitops"]["mode"] == "solo"


class TestIsDevPosture:
    def test_dev_postures(self):
        from dfe_engine.settings import is_dev_posture

        for env in ("dev", "development", "local", "test", "ci", " DEV "):
            assert is_dev_posture(env) is True

    def test_production_postures(self):
        from dfe_engine.settings import is_dev_posture

        for env in ("production", "prod", "staging", ""):
            assert is_dev_posture(env) is False


class TestProductionPostureCannotShipWithAuthOff:
    """The production posture must not be satisfiable with authorization off.

    ``DFESettings.env`` defaults to ``"production"`` and its own description calls
    that "default, secure". ``AuthSettings.enabled`` defaults to ``False``. Under
    that pair:

    * ``get_current_user`` path 4 (api/deps.py) returns
      ``AuthContext(user_id="dev", roles=["admin"])`` for a request carrying no
      credentials at all;
    * ``authorize(..., enabled=False)`` short-circuits to
      ``allowed=True, reason="auth_disabled"``, so every ``require_action`` and
      ``check_action`` on every REST handler passes.

    The weak-jwt-secret guard could never cover this: it is itself gated on
    ``self.auth.enabled``. Nothing logged and nothing raised, so the deployment
    looked configured and enforced nothing.

    Closed in two places. ``DFESettings`` refuses to load the pairing, and
    ``api/deps.py`` path 4 gates its root context on ``is_dev_posture(env)`` as
    well as the flag, for anything holding a settings object that did not come
    through the validator.

    The shipped Helm chart sets ``config.auth.enabled: true``, so a chart install
    was always covered. A plain container was not.
    """

    def test_production_posture_with_auth_off_refuses_to_load(self):
        with pytest.raises(ValidationError, match="grants every unauthenticated caller"):
            DFESettings(env="production", auth=AuthSettings(enabled=False))

    def test_dev_posture_with_auth_off_still_loads(self):
        # The opt-out has to keep working, or every local dev setup breaks.
        for env in ("dev", "development", "local", "test", "ci"):
            settings = DFESettings(env=env, auth=AuthSettings(enabled=False))
            assert settings.auth.enabled is False

    def test_shipped_defaults_enable_auth(self, monkeypatch):
        """defaults.yaml is the file a plain container runs on."""
        monkeypatch.setenv("DFE_ENV", "production")
        monkeypatch.setenv("DFE_API_JWT_SECRET", "a-real-production-secret-over-32-bytes")
        assert load_settings().auth.enabled is True

    def test_deps_root_context_condition_excludes_a_production_posture(self):
        """The second line, for a settings object that skipped the validator.

        ``api/deps.py`` path 4 returns ``AuthContext(user_id="dev",
        roles=["admin"])`` when ``not auth.enabled and is_dev_posture(env)``. The
        flag alone was the whole condition, so a production posture reached it.
        ``model_construct`` is how a settings object gets here without the
        validator above having run.
        """
        from dfe_engine.settings import is_dev_posture

        prod = DFESettings.model_construct(env="production", auth=AuthSettings(enabled=False))
        assert not (not prod.auth.enabled and is_dev_posture(prod.env))

        dev = DFESettings.model_construct(env="dev", auth=AuthSettings(enabled=False))
        assert not dev.auth.enabled
        assert is_dev_posture(dev.env)
