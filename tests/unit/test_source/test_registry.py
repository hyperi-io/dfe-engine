"""Tests for SourceRegistry — CRUD, validation, round-trip."""

import pytest

from dfe_engine.source.models import Source
from dfe_engine.source.registry import (
    SourceMatchConflictError,
    SourceNotFoundError,
    SourceRegistry,
    SourceValidationError,
)
from dfe_engine.yaml_utils import yaml_load


@pytest.fixture
def sources_dir(tmp_path):
    """Create a temporary sources directory."""
    d = tmp_path / "sources"
    d.mkdir()
    return d


@pytest.fixture
def registry(sources_dir) -> SourceRegistry:
    """Create a fresh SourceRegistry for each test."""
    SourceRegistry.reset_instance()
    reg = SourceRegistry(
        sources_directory=sources_dir,
        writable=True,
        refresh_interval=0,
    )
    yield reg
    reg.close()
    SourceRegistry.reset_instance()


def _make_source(name: str, match_value: str | None = None, **kwargs) -> Source:
    """Helper to create a Source with minimal fields."""
    data = {"source": name, **kwargs}
    if match_value:
        data["match"] = {"field": "tags.collector.type", "value": match_value}
    return Source.model_validate(data)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


class TestCRUD:
    def test_save_and_get(self, registry: SourceRegistry):
        source = _make_source("filebeat", match_value="filebeat")
        registry.save_source(source)

        loaded = registry.get_source("filebeat")
        assert loaded.source == "filebeat"
        assert loaded.match.value == "filebeat"

    def test_save_writes_versioned_yaml(self, registry: SourceRegistry, sources_dir):
        registry.save_source(
            {
                "source": "versioned_src",
                "display_name": "Versioned",
                "match": {"field": "tags.type", "value": "versioned"},
                "schema": {"ttl_days": 30},
            }
        )
        data = yaml_load(sources_dir / "versioned_src.yaml")
        assert "deployed_version" not in data
        assert data["current"] == "1.0.0"
        assert "1.0.0" in data["versions"]
        assert data["versions"]["1.0.0"]["schema"]["ttl_days"] == 30
        assert "header" not in data

    def test_save_from_dict(self, registry: SourceRegistry):
        registry.save_source(
            {
                "source": "syslog",
                "match": {"field": "tags.type", "value": "syslog"},
                "schema": {"ttl_days": 30},
            }
        )
        loaded = registry.get_source("syslog")
        assert loaded.source == "syslog"
        assert loaded.schema_config.ttl_days == 30

    def test_get_not_found(self, registry: SourceRegistry):
        with pytest.raises(SourceNotFoundError):
            registry.get_source("nonexistent")

    def test_update(self, registry: SourceRegistry):
        source = _make_source("filebeat", match_value="filebeat")
        registry.save_source(source)

        updated = _make_source(
            "filebeat",
            match_value="filebeat",
            description="Updated description",
        )
        registry.save_source(updated)

        loaded = registry.get_source("filebeat")
        assert loaded.description == "Updated description"

    def test_delete(self, registry: SourceRegistry):
        source = _make_source("filebeat", match_value="filebeat")
        registry.save_source(source)
        assert registry.source_exists("filebeat")

        registry.delete_source("filebeat")
        assert not registry.source_exists("filebeat")

    def test_delete_nonexistent(self, registry: SourceRegistry):
        # Should not raise
        registry.delete_source("nonexistent")

    def test_source_exists(self, registry: SourceRegistry):
        assert not registry.source_exists("filebeat")
        registry.save_source(_make_source("filebeat", match_value="filebeat"))
        assert registry.source_exists("filebeat")


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestList:
    def test_list_empty(self, registry: SourceRegistry):
        assert registry.list_sources() == []

    def test_list_multiple(self, registry: SourceRegistry):
        registry.save_source(_make_source("filebeat", match_value="filebeat"))
        registry.save_source(_make_source("syslog", match_value="syslog"))

        sources = registry.list_sources()
        names = {s["source"] for s in sources}
        assert names == {"filebeat", "syslog"}

    def test_list_enabled_only(self, registry: SourceRegistry):
        registry.save_source(_make_source("filebeat", match_value="filebeat"))
        registry.save_source(_make_source("disabled_src", match_value="disabled", enabled=False))

        all_sources = registry.list_sources()
        assert len(all_sources) == 2

        enabled_sources = registry.list_sources(enabled_only=True)
        assert len(enabled_sources) == 1
        assert enabled_sources[0]["source"] == "filebeat"

    def test_get_all_sources(self, registry: SourceRegistry):
        registry.save_source(_make_source("a", match_value="a"))
        registry.save_source(_make_source("b", match_value="b"))

        sources = registry.get_all_sources()
        assert len(sources) == 2
        assert all(isinstance(s, Source) for s in sources)

    def test_get_all_sources_enabled_only(self, registry: SourceRegistry):
        registry.save_source(_make_source("a", match_value="a"))
        registry.save_source(_make_source("b", match_value="b", enabled=False))

        sources = registry.get_all_sources(enabled_only=True)
        assert len(sources) == 1
        assert sources[0].source == "a"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_invalid_source_name(self, registry: SourceRegistry):
        with pytest.raises(SourceValidationError):
            registry.save_source({"source": "INVALID-NAME"})

    def test_match_conflict(self, registry: SourceRegistry):
        registry.save_source(_make_source("filebeat", match_value="fb"))

        with pytest.raises(SourceMatchConflictError) as exc_info:
            registry.save_source(_make_source("another_source", match_value="fb"))
        assert exc_info.value.conflicting_source == "filebeat"
        assert exc_info.value.source == "another_source"

    def test_match_conflict_same_source_ok(self, registry: SourceRegistry):
        """Updating the same source should not conflict with itself."""
        source = _make_source("filebeat", match_value="fb")
        registry.save_source(source)
        # Save again (update) — should not raise
        registry.save_source(source)

    def test_match_conflict_disabled_ok(self, registry: SourceRegistry):
        """Disabled sources don't participate in match conflict checks."""
        registry.save_source(_make_source("filebeat", match_value="fb", enabled=False))
        # Same match value but first source is disabled — should be OK
        registry.save_source(_make_source("another", match_value="fb"))


# ---------------------------------------------------------------------------
# Match Table
# ---------------------------------------------------------------------------


class TestMatchTable:
    def test_compile_empty(self, registry: SourceRegistry):
        assert registry.compile_match_table() == []

    def test_compile_multiple(self, registry: SourceRegistry):
        registry.save_source(_make_source("filebeat", match_value="filebeat"))
        registry.save_source(_make_source("syslog", match_value="syslog"))

        table = registry.compile_match_table()
        assert len(table) == 2
        values = {r["source"] for r in table}
        assert values == {"filebeat", "syslog"}

    def test_compile_excludes_disabled(self, registry: SourceRegistry):
        registry.save_source(_make_source("filebeat", match_value="filebeat"))
        registry.save_source(_make_source("disabled", match_value="disabled", enabled=False))

        table = registry.compile_match_table()
        assert len(table) == 1
        assert table[0]["source"] == "filebeat"


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_save_load_full_source(self, registry: SourceRegistry):
        source = Source.model_validate(
            {
                "source": "crowdstrike_edr",
                "display_name": "CrowdStrike EDR",
                "description": "CrowdStrike Falcon EDR telemetry",
                "enabled": True,
                "header": {"type": "time_series", "version": "1.0.0"},
                "match": {"field": "tags.vendor", "value": "crowdstrike"},
                "schema": {
                    "meta_schema": "security_edr_crowdstrike",
                    "meta_schema_version": "1.0.0",
                    "ttl_days": 365,
                    "engine": "ReplicatedMergeTree",
                },
                "transform": {
                    "engine": "vector",
                    "config_file": "/etc/vector/crowdstrike.yaml",
                    "env": {"CS_API_KEY": "secret"},
                    "files": ["/data/enrichment/geo.mmdb"],
                },
                "fetcher": {
                    "source_type": "crowdstrike",
                    "base_url": "https://api.crowdstrike.com",
                    "auth": {
                        "type": "oauth2",
                        "token_url": "https://api.crowdstrike.com/oauth2/token",
                    },
                    "poll_interval_secs": 60,
                },
                "sigma": {
                    "taxonomy": "windows",
                    "custom_mappings": {
                        "CommandLine": "command_line",
                        "ParentCommandLine": "parent_cmd",
                    },
                },
            }
        )

        registry.save_source(source)
        loaded = registry.get_source("crowdstrike_edr")

        assert loaded.source == source.source
        assert loaded.display_name == source.display_name
        assert loaded.description == source.description
        assert loaded.enabled == source.enabled
        assert loaded.header.type == source.header.type
        assert loaded.match.field == source.match.field
        assert loaded.match.value == source.match.value
        assert loaded.schema_config.meta_schema == source.schema_config.meta_schema
        assert loaded.schema_config.ttl_days == source.schema_config.ttl_days
        assert loaded.schema_config.engine == source.schema_config.engine
        assert loaded.transform.engine == source.transform.engine
        assert loaded.transform.config_file == source.transform.config_file
        assert loaded.fetcher.source_type == source.fetcher.source_type
        assert loaded.fetcher.auth.type == source.fetcher.auth.type
        assert loaded.sigma.taxonomy == source.sigma.taxonomy
        assert loaded.sigma.custom_mappings == source.sigma.custom_mappings


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_instance_requires_dir(self):
        SourceRegistry.reset_instance()
        with pytest.raises(Exception, match="sources_directory is required"):
            SourceRegistry.get_instance()

    def test_get_instance_returns_same(self, sources_dir):
        SourceRegistry.reset_instance()
        try:
            r1 = SourceRegistry.get_instance(sources_directory=sources_dir)
            r2 = SourceRegistry.get_instance()
            assert r1 is r2
        finally:
            SourceRegistry.reset_instance()


# ---------------------------------------------------------------------------
# Path traversal (F-SOURCES-TRAVERSAL)
# ---------------------------------------------------------------------------


class TestPathTraversal:
    """A source name is a single flat filename stem. A name with ``..``, a path
    separator or a NUL byte must never let get/save/delete read/write/unlink a
    file OUTSIDE the sources directory - e.g. bulk-deleting
    ``../../governance/rbac/roles/admin`` unlinking that policy file."""

    @pytest.mark.parametrize(
        "bad",
        [
            "../victim",
            "../../governance/rbac/roles/admin",
            "a/b",
            "..",
            ".",
            "a\x00b",
            "a\\b",
        ],
    )
    def test_get_and_delete_reject_traversal_names(self, registry: SourceRegistry, bad):
        with pytest.raises(SourceValidationError):
            registry.get_source(bad)
        with pytest.raises(SourceValidationError):
            registry.delete_source(bad)

    def test_delete_traversal_does_not_unlink_outside_file(
        self, registry: SourceRegistry, sources_dir
    ):
        victim = sources_dir.parent / "victim.yaml"
        victim.write_text("keep: me\n", encoding="utf-8")
        with pytest.raises(SourceValidationError):
            registry.delete_source("../victim")
        assert victim.exists()

    def test_normal_name_still_round_trips(self, registry: SourceRegistry):
        registry.save_source(_make_source("normal_src", match_value="normal_src"))
        assert registry.get_source("normal_src").source == "normal_src"
        registry.delete_source("normal_src")
        assert not registry.source_exists("normal_src")
