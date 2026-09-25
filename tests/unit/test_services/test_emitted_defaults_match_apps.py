"""An engine default is what the Rust app runs on, so it must match the app's.

``ServiceConfigRegistry.save_config`` validates the operator's partial dict and
dumps the whole model, and ``HelmValuesCompiler._compile_service_config`` does
the same. Every key the operator left alone is therefore WRITTEN into the config
the app reads, and the app's own ``#[serde(default)]`` never gets a look in --
so an engine default that drifts from the app's silently replaces it.

The values below are pinned to the upstream Rust ``impl Default``. Changing one
here without changing it there re-opens the drift; the citation is the commit to
check it against.
"""

import json
from pathlib import Path

import pytest

from dfe_engine.services.models.archiver import ArchiverConfig
from dfe_engine.services.models.loader import LoaderConfig
from dfe_engine.services.models.receiver import ReceiverConfig
from dfe_engine.services.registry import ServiceConfigRegistry
from dfe_engine.services.templates import generate_template
from dfe_engine.yaml_utils import yaml_load

FIXTURES = Path(__file__).parents[2] / "fixtures"


def _schema_default(fixture_dir: str, app: str, section: str) -> dict:
    """The default an app's emitted schema gives one top-level section."""
    schema = json.loads((FIXTURES / fixture_dir / app / "config-schema.json").read_text())
    return schema["properties"][section]["default"]


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
    """dfe-archiver picks 300 s while it holds Kafka offsets and 3600 s otherwise.

    It can only pick when the key is absent, and a dump writes every field, so an
    unset interval has to leave the key out rather than write null or a number.
    """

    def test_the_app_leaves_it_unset(self):
        app = _schema_default("contract-acknowledgements", "dfe-archiver", "archive")
        assert app["roll_interval_secs"] is None
        assert ArchiverConfig().archive.roll_interval_secs is None

    def test_an_untouched_config_dumps_without_it(self):
        assert "roll_interval_secs" not in ArchiverConfig().model_dump(mode="json")["archive"]

    @pytest.mark.parametrize("profile", ["default", "production", "k8s"])
    def test_no_emitted_template_writes_it(self, profile: str):
        emitted = generate_template("archiver", profile=profile)
        assert "roll_interval_secs" not in emitted["archive"]

    def test_an_operator_value_is_kept(self):
        config = ArchiverConfig.model_validate({"archive": {"roll_interval_secs": 900}})
        assert config.model_dump(mode="json")["archive"]["roll_interval_secs"] == 900

    def test_a_saved_config_round_trips_without_it(self, tmp_path):
        """The /services path: validate the operator's dict, dump the model, write YAML."""
        registry = ServiceConfigRegistry(config_directory=tmp_path, writable=True)
        try:
            registry.save_config("archiver", {"archive": {"roll_size_bytes": 1024}}, "unset")
            registry.save_config("archiver", {"archive": {"roll_interval_secs": 900}}, "set")
            unset = yaml_load(tmp_path / "archiver-unset.yaml")["archive"]
            written = yaml_load(tmp_path / "archiver-set.yaml")["archive"]
            read_back = registry.get_config("archiver", "set").archive.roll_interval_secs
        finally:
            registry.close()
        assert "roll_interval_secs" not in unset
        assert written["roll_interval_secs"] == 900
        assert read_back == 900

    def test_a_zero_interval_is_refused(self):
        with pytest.raises(ValueError, match="greater than 0"):
            ArchiverConfig.model_validate({"archive": {"roll_interval_secs": 0}})


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


class TestLoaderKafkaDefaults:
    """dfe-loader src/config/kafka.rs, as the pinned image's emitted schema states it."""

    @pytest.mark.parametrize("field", ["group", "client_id", "topics"])
    def test_the_consumer_identity_is_the_app_s(self, field: str):
        app = _schema_default("contract", "dfe-loader", "kafka")[field]
        assert getattr(LoaderConfig().kafka, field) == app

    def test_an_empty_topic_list_is_the_default(self):
        """Empty is how the loader is told to auto-discover its *_load/*_land topics."""
        assert LoaderConfig().kafka.topics == []


class TestReceiverNextHopDeadline:
    """dfe-receiver src/config/mod.rs, from the build that holds each answer."""

    def test_the_loader_deadline_is_the_app_s(self):
        app = _schema_default("contract-acknowledgements", "dfe-receiver", "loader")
        assert ReceiverConfig().loader.timeout_ms == app["timeout_ms"]

    def test_it_sits_inside_the_listener_s_hold(self):
        """A 25 s hold answered before a 20 s send settles would retry a delivered batch."""
        assert ReceiverConfig().loader.timeout_ms < 25_000
