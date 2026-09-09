"""Tests for SourceRegistry — CRUD, validation, round-trip."""

import pytest

from dfe_engine.schema.schema_builder_v2 import SchemaBuildResult
from dfe_engine.source.deployment import SourceDeploymentStore, artifact_from_build
from dfe_engine.source.models import Source, SourceWriteRequest
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
                "source": "versioned-src",
                "display_name": "Versioned",
                "match": {"field": "tags.type", "value": "versioned"},
                "schema": {"ttl_days": 30},
            }
        )
        data = yaml_load(sources_dir / "versioned-src.yaml")
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
        registry.save_source(_make_source("disabled-src", match_value="disabled", enabled=False))

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

    def test_get_all_sources_states_filter(self, registry: SourceRegistry):
        """Tri-state filter: DDL compile wants active+dormant, routing wants active."""
        registry.save_source(_make_source("act", match_value="act", state="active"))
        registry.save_source(_make_source("dor", match_value="dor", state="dormant"))
        registry.save_source(_make_source("dis", match_value="dis", state="disabled"))

        ddl_set = {s.source for s in registry.get_all_sources(states=("active", "dormant"))}
        assert ddl_set == {"act", "dor"}

        routing_set = {s.source for s in registry.get_all_sources(states=("active",))}
        assert routing_set == {"act"}

        # compat: enabled_only == active only
        assert {s.source for s in registry.get_all_sources(enabled_only=True)} == {"act"}

    def test_dormant_match_still_conflicts(self, registry: SourceRegistry):
        """A dormant source holds its receiver match (it may activate later)."""
        registry.save_source(_make_source("dor", match_value="shared", state="dormant"))
        with pytest.raises(SourceMatchConflictError):
            registry.save_source(_make_source("act", match_value="shared", state="active"))

    def test_unsupported_match_operator_rejected_on_save(self, registry: SourceRegistry):
        """Receiver-routed sources must use equals/exists (documented receiver gap)."""
        bad = Source.model_validate(
            {
                "source": "bad-op",
                "match": {"field": "f", "operator": "includes", "value": "v"},
            }
        )
        with pytest.raises(SourceValidationError, match="documented receiver gap"):
            registry.save_source(bad)

    def test_disabled_source_with_legacy_operator_saves(self, registry: SourceRegistry):
        """Disabling is how an operator retires a legacy-operator source - the
        operator gate must not block that exit path (active/dormant still reject)."""
        legacy = Source.model_validate(
            {
                "source": "legacy-op",
                "state": "disabled",
                "match": {"field": "f", "operator": "includes", "value": "v"},
            }
        )
        registry.save_source(legacy)
        assert registry.get_source("legacy-op").state == "disabled"

    def test_always_is_reserved_for_the_default_source(self, registry: SourceRegistry):
        """It matches every record, so on any other source it would shadow the rest."""
        greedy = Source.model_validate(
            {"source": "greedy", "match": {"field": "_source", "operator": "always"}}
        )
        with pytest.raises(SourceValidationError, match="reserved for the 'default' source"):
            registry.save_source(greedy)

    def test_the_default_source_may_match_everything(self, registry: SourceRegistry):
        registry.save_source(
            Source.model_validate(
                {"source": "default", "match": {"field": "_source", "operator": "always"}}
            )
        )
        assert registry.get_source("default").match.operator == "always"

    def test_a_fetcher_route_may_not_name_its_own_source(self, registry: SourceRegistry):
        looping = Source.model_validate(
            {
                "source": "okta",
                "fetcher": {
                    "source_type": "okta",
                    "routes": [{"match": {"field": "eventType", "value": "x"}, "source": "okta"}],
                },
            }
        )
        with pytest.raises(SourceValidationError, match="names its own source"):
            registry.save_source(looping)

    def test_a_route_to_another_source_saves_before_that_source_exists(
        self, registry: SourceRegistry
    ):
        """A route may legitimately be written before its target; compile checks it."""
        registry.save_source(
            Source.model_validate(
                {
                    "source": "okta",
                    "fetcher": {
                        "source_type": "okta",
                        "routes": [
                            {"match": {"field": "eventType", "value": "x"}, "source": "audit"}
                        ],
                    },
                }
            )
        )
        assert registry.get_source("okta").fetcher.routes[0].source == "audit"

    def test_archive_on_the_direct_transport_is_accepted(self, registry: SourceRegistry):
        # The archiver declares direct and a Push listener, so the save that once
        # named the refusal now lands the source.
        direct = Source.model_validate(
            {
                "source": "auth",
                "match": {"field": "_source", "value": "auth"},
                "transport": "direct",
                "archive": True,
            }
        )
        registry.save_source(direct)

        assert registry.get_source("auth").archive is True

    def test_a_transform_that_does_not_carry_the_transport_is_refused(
        self, registry: SourceRegistry
    ):
        direct = Source.model_validate(
            {
                "source": "auth",
                "match": {"field": "_source", "value": "auth"},
                "transport": "direct",
                "transform": {"engine": "elastic"},
            }
        )
        with pytest.raises(SourceValidationError, match="carries only bus"):
            registry.save_source(direct)

    def test_disabling_is_the_exit_from_a_flow_the_deployment_cannot_run(
        self, registry: SourceRegistry
    ):
        stuck = Source.model_validate(
            {
                "source": "auth",
                "state": "disabled",
                "match": {"field": "_source", "value": "auth"},
                "transport": "direct",
                "archive": True,
            }
        )
        registry.save_source(stuck)
        assert registry.get_source("auth").state == "disabled"

    def test_disabled_releases_match(self, registry: SourceRegistry):
        registry.save_source(_make_source("dis", match_value="shared", state="disabled"))
        registry.save_source(_make_source("act", match_value="shared", state="active"))
        assert registry.get_source("act").state == "active"

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
            registry.save_source(_make_source("another-source", match_value="fb"))
        assert exc_info.value.conflicting_source == "filebeat"
        assert exc_info.value.source == "another-source"

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
                "source": "crowdstrike-edr",
                "display_name": "CrowdStrike EDR",
                "description": "CrowdStrike Falcon EDR telemetry",
                "enabled": True,
                "header": {"type": "timeseries", "version": "1.0.0"},
                "match": {"field": "tags.vendor", "value": "crowdstrike"},
                "schema": {
                    "meta_schema": "security_edr_crowdstrike",
                    "meta_schema_version": "1.0.0",
                    "ttl_days": 365,
                    "engine": "ReplacingMergeTree",
                },
                "transform": {
                    "engine": "vector",
                    "config_file": "/etc/vector/crowdstrike.yaml",
                    "env": {"CS_API_KEY": "secret"},
                    "files": ["/data/enrichment/geo.mmdb"],
                },
                "views": [
                    {
                        "standard": "sigma",
                        "taxonomy": "windows",
                        "custom_mappings": {
                            "CommandLine": "command_line",
                            "ParentCommandLine": "parent_cmd",
                        },
                    }
                ],
            }
        )

        registry.save_source(source)
        loaded = registry.get_source("crowdstrike-edr")

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
        assert loaded.fetcher is None
        assert loaded.view_for("sigma").taxonomy == source.view_for("sigma").taxonomy
        assert loaded.view_for("sigma").custom_mappings == source.view_for("sigma").custom_mappings

    def test_save_load_fetcher_source(self, registry: SourceRegistry):
        source = Source.model_validate(
            {
                "source": "crowdstrike-edr",
                "fetcher": {
                    "source_type": "crowdstrike",
                    "topic": "own",
                    "config": {
                        "credential_secret": "vault:secret/crowdstrike:client_secret",
                        "services": [{"name": "alerts"}],
                        "interval_secs": 60,
                    },
                },
            }
        )

        registry.save_source(source)
        loaded = registry.get_source("crowdstrike-edr")

        assert loaded.origin == "fetcher"
        assert loaded.match is None
        assert loaded.fetcher.source_type == "crowdstrike"
        assert loaded.fetcher.config == source.fetcher.config
        assert registry.list_sources()[0]["origin"] == "fetcher"

    def test_update_draft_drops_stale_source_build(self, registry: SourceRegistry, tmp_path):
        store = SourceDeploymentStore(
            builds_dir=tmp_path / "builds",
            plans_dir=tmp_path / "plans",
            deploys_dir=tmp_path / "deploys",
        )
        registry.save_source(
            {
                "source": "draft-src",
                "match": {"field": "f", "value": "v"},
                "deployed_version": "1.0.0",
                "current": "2.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "match": {"field": "f", "value": "v"},
                        "schema": {"meta_schema": "meta/a", "meta_schema_version": "1.0.0"},
                    },
                    "2.0.0": {
                        "date_time": "2026-01-02",
                        "match": {"field": "f", "value": "v"},
                        "schema": {"meta_schema": "meta/a", "meta_schema_version": "1.0.0"},
                    },
                },
            }
        )
        source = registry.get_source("draft-src")
        store.save_build(
            artifact_from_build(
                SchemaBuildResult(
                    source_name="draft-src",
                    columns=[],
                    create_table_ddl="CREATE TABLE t",
                ),
                version="2.0.0",
            ),
            source,
        )
        registry.update_source_from_write(
            "draft-src",
            SourceWriteRequest.model_validate(
                {
                    "match": {"field": "f", "value": "v"},
                    "schema": {
                        "meta_schema": "meta/b",
                        "meta_schema_version": "1.0.0",
                    },
                }
            ),
            deployment_store=store,
        )
        assert store.load_build("draft-src", "2.0.0") is None
        assert store.load_build("draft-src", "1.0.0") is None

    def test_update_pre_deploy_drops_stale_source_build(self, registry: SourceRegistry, tmp_path):
        store = SourceDeploymentStore(
            builds_dir=tmp_path / "builds",
            plans_dir=tmp_path / "plans",
            deploys_dir=tmp_path / "deploys",
        )
        registry.save_source(
            {
                "source": "pre-deploy",
                "match": {"field": "f", "value": "v"},
                "deployed_version": None,
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "match": {"field": "f", "value": "v"},
                        "schema": {"meta_schema": "meta/a", "meta_schema_version": "1.0.0"},
                    }
                },
            }
        )
        source = registry.get_source("pre-deploy")
        store.save_build(
            artifact_from_build(
                SchemaBuildResult(
                    source_name="pre-deploy",
                    columns=[],
                    create_table_ddl="CREATE TABLE t",
                ),
                version="1.0.0",
            ),
            source,
        )
        registry.update_source_from_write(
            "pre-deploy",
            SourceWriteRequest.model_validate(
                {
                    "match": {"field": "f", "value": "v"},
                    "schema": {
                        "meta_schema": "meta/b",
                        "meta_schema_version": "1.0.0",
                    },
                }
            ),
            deployment_store=store,
        )
        assert store.load_build("pre-deploy", "1.0.0") is None


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
