#  Project:      dfe-engine
#  File:         tests/unit/test_sigma/test_sigma_views.py
#  Purpose:      CRUD-managed Sigma source-view definitions + JSON-derived DDL
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""SigmaViewDefinition model + build_sigma_view_ddl + SigmaViewStore.

No mocks: a real local (no-remote) dulwich repo and the real gitcrud engine, so
the per-source view definition round-trips through git and the JSON-derived DDL is
exercised end to end. The DDL is asserted as a string only (no live ClickHouse).
"""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud import GitCrud, ResourceNotFoundError, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.sigma.views import (
    SigmaViewColumn,
    SigmaViewDefinition,
    SigmaViewError,
    SigmaViewStore,
    build_sigma_view_ddl,
)
from dfe_engine.source.models import Source
from dfe_engine.source.registry import SourceNotFoundError


@pytest.fixture
def crud(tmp_path) -> GitCrud:
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    return GitCrud(repo, default_registry())


def _definition(**overrides) -> SigmaViewDefinition:
    data = {
        "source_name": "windows_audit",
        "description": "Windows process creation",
        "columns": [
            {"sigma_field": "EventID", "json_path": "EventID", "type": "UInt32"},
            {"sigma_field": "CommandLine", "json_path": "process.command_line"},
            {"sigma_field": "Image", "source_column": "process_name"},
        ],
    }
    data.update(overrides)
    return SigmaViewDefinition.model_validate(data)


# ── Model validation ────────────────────────────────────────


def test_column_requires_exactly_one_source_kind():
    # both source_column and json_path -> rejected
    with pytest.raises(ValueError, match="exactly one"):
        SigmaViewColumn(sigma_field="X", source_column="c", json_path="p")
    # neither -> rejected
    with pytest.raises(ValueError, match="exactly one"):
        SigmaViewColumn(sigma_field="X")


def test_json_derived_flag_and_subset():
    definition = _definition()
    assert {c.sigma_field for c in definition.json_derived_columns} == {"EventID", "CommandLine"}
    assert definition.columns[2].is_json_derived is False


# ── DDL: source columns ─────────────────────────────────────


def test_build_ddl_source_column_aliases():
    definition = SigmaViewDefinition(
        source_name="win",
        columns=[SigmaViewColumn(sigma_field="Image", source_column="process_name")],
        include_source_columns=False,
    )
    ddl = build_sigma_view_ddl(definition, db="default")
    assert "CREATE OR REPLACE VIEW default.win_sigma AS" in ddl
    assert "`process_name` AS `Image`" in ddl
    assert "FROM default.win;" in ddl
    # include_source_columns=False -> no trailing SELECT *
    assert "\n    *" not in ddl


# ── DDL: JSON-derived columns (Task B core) ─────────────────


def test_build_ddl_json_derived_extracts_from_json():
    """A JSON-derived column extracts from _json with the dynamic-subcolumn idiom."""
    definition = SigmaViewDefinition(
        source_name="win",
        columns=[SigmaViewColumn(sigma_field="EventID", json_path="EventID")],
    )
    ddl = build_sigma_view_ddl(definition, db="default")
    # assumeNotNull(_json).`path` AS `SigmaField` — matches json_promotion_service idiom
    assert "assumeNotNull(_json).`EventID` AS `EventID`" in ddl


def test_build_ddl_json_derived_with_type_casts():
    definition = SigmaViewDefinition(
        source_name="win",
        columns=[SigmaViewColumn(sigma_field="EventID", json_path="EventID", type="UInt32")],
    )
    ddl = build_sigma_view_ddl(definition, db="default")
    assert "CAST(assumeNotNull(_json).`EventID` AS UInt32) AS `EventID`" in ddl


def test_build_ddl_nested_json_path_is_single_quoted_identifier():
    definition = SigmaViewDefinition(
        source_name="win",
        columns=[SigmaViewColumn(sigma_field="CommandLine", json_path="process.command_line")],
    )
    ddl = build_sigma_view_ddl(definition, db="default")
    # the WHOLE dotted path is one backtick-quoted identifier (JSON subcolumn access)
    assert "assumeNotNull(_json).`process.command_line` AS `CommandLine`" in ddl


def test_build_ddl_mixed_columns_and_star():
    definition = _definition()
    ddl = build_sigma_view_ddl(definition, db="dfe", table_name="windows_audit")
    assert "CREATE OR REPLACE VIEW dfe.windows_audit_sigma AS" in ddl
    assert "CAST(assumeNotNull(_json).`EventID` AS UInt32) AS `EventID`" in ddl
    assert "assumeNotNull(_json).`process.command_line` AS `CommandLine`" in ddl
    assert "`process_name` AS `Image`" in ddl
    assert ddl.rstrip().endswith("FROM dfe.windows_audit;")
    # include_source_columns default True -> SELECT * retained
    assert "    *," in ddl or "    *\n" in ddl


def test_build_ddl_empty_definition_selects_star():
    definition = SigmaViewDefinition(source_name="win", include_source_columns=False)
    ddl = build_sigma_view_ddl(definition, db="default")
    assert "SELECT\n    *\nFROM default.win;" in ddl


def test_build_ddl_default_db_placeholder():
    definition = SigmaViewDefinition(
        source_name="win",
        columns=[SigmaViewColumn(sigma_field="X", source_column="x")],
    )
    ddl = build_sigma_view_ddl(definition)
    assert "{db}.win_sigma" in ddl
    assert "FROM {db}.win;" in ddl


# ── DDL: injection safety ───────────────────────────────────


def test_build_ddl_rejects_backtick_in_json_path():
    definition = SigmaViewDefinition(
        source_name="win",
        columns=[SigmaViewColumn(sigma_field="X", json_path="a`b")],
    )
    with pytest.raises(SigmaViewError, match="backtick"):
        build_sigma_view_ddl(definition, db="default")


def test_build_ddl_rejects_backtick_in_alias():
    definition = SigmaViewDefinition(
        source_name="win",
        columns=[SigmaViewColumn(sigma_field="a`b", source_column="x")],
    )
    with pytest.raises(SigmaViewError, match="sigma_field"):
        build_sigma_view_ddl(definition, db="default")


def test_build_ddl_rejects_illegal_ch_type():
    definition = SigmaViewDefinition(
        source_name="win",
        columns=[SigmaViewColumn(sigma_field="X", json_path="p", type="String; DROP")],
    )
    with pytest.raises(SigmaViewError, match="ClickHouse type"):
        build_sigma_view_ddl(definition, db="default")


# ── Store CRUD over a real gitcrud repo ─────────────────────


def test_store_save_get_roundtrip(crud):
    store = SigmaViewStore(crud)
    saved = store.save(_definition(), actor="alice")
    assert saved.source_name == "windows_audit"

    loaded = store.get("windows_audit")
    assert loaded.source_name == "windows_audit"
    assert len(loaded.columns) == 3
    assert loaded.columns[0].json_path == "EventID"
    assert loaded.columns[0].type == "UInt32"


def test_store_save_is_one_commit(crud):
    store = SigmaViewStore(crud)
    head_before = crud.head_revision()
    store.save(_definition(), actor="alice")
    head_after = crud.head_revision()
    assert head_after is not None
    assert head_after != head_before


def test_store_get_missing_raises(crud):
    store = SigmaViewStore(crud)
    assert store.exists("nope") is False
    with pytest.raises(ResourceNotFoundError):
        store.get("nope")


def test_store_list_and_summaries(crud):
    store = SigmaViewStore(crud)
    store.save(_definition(), actor="a")
    store.save(
        SigmaViewDefinition(
            source_name="linux_syslog",
            columns=[SigmaViewColumn(sigma_field="exe", source_column="process_name")],
        ),
        actor="a",
    )
    assert set(store.list_sources()) == {"windows_audit", "linux_syslog"}

    summaries = {s["source_name"]: s for s in store.summaries()}
    assert summaries["windows_audit"]["column_count"] == 3
    assert summaries["windows_audit"]["json_derived_count"] == 2
    assert summaries["linux_syslog"]["json_derived_count"] == 0


def test_store_delete(crud):
    store = SigmaViewStore(crud)
    store.save(_definition(), actor="a")
    assert store.exists("windows_audit")
    store.delete("windows_audit", actor="op")
    assert not store.exists("windows_audit")
    with pytest.raises(ResourceNotFoundError):
        store.delete("windows_audit", actor="op")


def test_store_generate_ddl_from_stored_definition(crud):
    """generate_ddl reads the stored definition and emits JSON-derived extraction."""
    store = SigmaViewStore(crud)
    store.save(_definition(), actor="a")
    ddl = store.generate_ddl("windows_audit", db="default")
    assert "CREATE OR REPLACE VIEW default.windows_audit_sigma AS" in ddl
    assert "CAST(assumeNotNull(_json).`EventID` AS UInt32) AS `EventID`" in ddl
    assert "`process_name` AS `Image`" in ddl


# ── SigmaSourceMapper prefers a stored definition (Task A wiring) ────


class _FakeSourceRegistry:
    def __init__(self, sources: list[Source]) -> None:
        self._sources = {s.source: s for s in sources}

    def get_source(self, name: str) -> Source:
        if name not in self._sources:
            raise SourceNotFoundError(f"Source '{name}' not found")
        return self._sources[name]

    def get_all_sources(self, enabled_only: bool = False) -> list[Source]:
        return list(self._sources.values())


def _source(name: str, mappings: dict[str, str]) -> Source:
    return Source.model_validate(
        {
            "source": name,
            "enabled": True,
            "match": {"field": "tags.collector.type", "value": name},
            "schema": {"engine": "MergeTree"},
            "sigma": {"taxonomy": "windows", "custom_mappings": mappings},
        }
    )


def test_source_mapper_prefers_stored_view_definition(crud):
    from dfe_engine.sigma.source_mapper import SigmaSourceMapper

    store = SigmaViewStore(crud)
    store.save(_definition(source_name="windows_audit"), actor="a")

    registry = _FakeSourceRegistry([_source("windows_audit", {"EventID": "legacy_col"})])
    mapper = SigmaSourceMapper(registry, view_store=store)

    ddl = mapper.generate_sigma_view("windows_audit", db="default")
    # stored def wins: JSON-derived extraction present, legacy field-map alias absent
    assert "CAST(assumeNotNull(_json).`EventID` AS UInt32) AS `EventID`" in ddl
    assert "legacy_col" not in ddl


def test_source_mapper_falls_back_to_field_maps_without_definition(crud):
    from dfe_engine.sigma.source_mapper import SigmaSourceMapper

    store = SigmaViewStore(crud)  # empty store, no definition for the source
    registry = _FakeSourceRegistry([_source("windows_audit", {"EventID": "event_id"})])
    mapper = SigmaSourceMapper(registry, view_store=store)

    ddl = mapper.generate_sigma_view("windows_audit", db="default")
    # legacy field-map path: real-column -> Sigma-field alias
    assert "`event_id` AS `EventID`" in ddl
    assert "assumeNotNull(_json)" not in ddl
