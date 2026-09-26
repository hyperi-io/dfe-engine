"""An engine default is what the Rust app runs on, so it must match the app's.

``ServiceConfigRegistry.save_config`` validates the operator's partial dict and
dumps the whole model, and ``HelmValuesCompiler._compile_service_config`` does
the same. Every key the operator left alone is therefore WRITTEN into the config
the app reads, and the app's own ``#[serde(default)]`` never gets a look in --
so an engine default that drifts from the app's silently replaces it.

The values below are pinned to the upstream Rust ``impl Default``. Changing one
here without changing it there re-opens the drift; the citation is the commit to
check it against. Where the app's default depends on how it is running, the
engine writes nothing and the app decides.
"""

from __future__ import annotations

import pytest

from dfe_engine.services.models.archiver import ArchiverConfig
from dfe_engine.services.models.loader import LoaderConfig
from dfe_engine.services.registry import ServiceConfigRegistry
from dfe_engine.services.templates import generate_template
from dfe_engine.yaml_utils import yaml_load


class TestArchiverDefaults:
    """dfe-archiver crates/core/src/config.rs."""

    def test_routing_splits_per_org(self):
        """RoutingConfig::default, dfe-archiver 3d04ac4."""
        routing = ArchiverConfig().routing
        assert routing.mode == "expression"
        assert routing.expression_fields == ["org_id"]

    def test_path_template_omits_topic(self):
        """The per-topic writers prepend it; a template carrying it doubles it."""
        assert ArchiverConfig().archive.path_template == "{year}/{month}/{day}/{hour}"

    def test_emitted_template_carries_them(self):
        """What an operator who touched nothing actually gets written."""
        emitted = generate_template("archiver", profile="default")
        assert emitted["routing"]["mode"] == "expression"
        assert emitted["routing"]["expression_fields"] == ["org_id"]
        assert emitted["archive"]["path_template"] == "{year}/{month}/{day}/{hour}"


class TestArchiverRollInterval:
    """dfe-archiver #98: unset is 300 s while offsets are held, 3600 s otherwise.

    So the engine writes a roll interval only when an operator set one.
    """

    @pytest.mark.parametrize("profile", ["default", "production", "k8s"])
    def test_emitted_template_omits_it(self, profile: str):
        emitted = generate_template("archiver", profile=profile)
        assert "roll_interval_secs" not in emitted["archive"]

    def test_saved_config_omits_it(self, tmp_path):
        """The YAML the registry writes on save, not only the in-memory dump."""
        services = tmp_path / "services"
        reg = ServiceConfigRegistry(config_directory=services, refresh_interval=0)
        try:
            reg.save_config("archiver", {"archive": {"destination": "file:///var/data/archive"}})
        finally:
            reg.close()
        written = yaml_load(services / "archiver-default.yaml")
        assert written["archive"]["destination"] == "file:///var/data/archive"
        assert "roll_interval_secs" not in written["archive"]

    def test_a_set_value_is_saved_as_given(self, tmp_path):
        services = tmp_path / "services"
        reg = ServiceConfigRegistry(config_directory=services, refresh_interval=0)
        try:
            reg.save_config("archiver", {"archive": {"roll_interval_secs": 1800}})
            reread = reg.get_config("archiver", "default")
        finally:
            reg.close()
        written = yaml_load(services / "archiver-default.yaml")
        assert written["archive"]["roll_interval_secs"] == 1800
        assert reread.archive.roll_interval_secs == 1800

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_non_positive_value_is_refused(self, bad: int):
        with pytest.raises(ValueError, match="greater than 0"):
            ArchiverConfig.model_validate({"archive": {"roll_interval_secs": bad}})


class TestLoaderClickHouseDefaults:
    """dfe-loader src/config/loader.rs."""

    def test_protocol_is_http(self):
        """ClickHouseConfig::default, dfe-loader a197f2c (#116, closes #115)."""
        ch = LoaderConfig().clickhouse
        assert ch.protocol == "http"
        assert ch.hosts == ["localhost:8123"]
        assert ch.database == "dfe"

    def test_native_is_rejected(self):
        """The loader refuses to start on it, so the engine must not bless it."""
        with pytest.raises(ValueError, match="no TCP row fetch"):
            LoaderConfig(clickhouse={"protocol": "native"})

    def test_unknown_protocol_is_rejected(self):
        with pytest.raises(ValueError, match=r"unknown clickhouse\.protocol"):
            LoaderConfig(clickhouse={"protocol": "mystery"})

    def test_emitted_template_carries_http(self):
        emitted = generate_template("loader", profile="default")
        assert emitted["clickhouse"]["protocol"] == "http"

    @pytest.mark.parametrize("profile", ["default", "production", "k8s"])
    def test_every_profile_pairs_http_with_an_http_port(self, profile: str):
        """The loader validates the pairing, so a profile cannot split them."""
        emitted = generate_template("loader", profile=profile)
        assert emitted["clickhouse"]["protocol"] == "http"
        for host in emitted["clickhouse"]["hosts"]:
            assert not host.endswith(":9000"), f"{profile} points http at a native port"
