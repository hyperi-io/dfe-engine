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

from __future__ import annotations

import pytest

from dfe_engine.services.models.archiver import ArchiverConfig
from dfe_engine.services.models.loader import LoaderConfig
from dfe_engine.services.templates import generate_template


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
