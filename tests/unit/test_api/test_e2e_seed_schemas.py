#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_e2e_seed_schemas.py
#  Purpose:      Searchable schema seeds stay outside the schema tree and any git worktree
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from pathlib import Path

import pytest

from dfe_engine.api.conventions import apply_search
from dfe_engine.auth.bootstrap import bootstrap_auth
from dfe_engine.orgs.registry import OrgRegistry
from dfe_engine.schema.registry import SchemaError, SchemaRegistry

_REPO = Path(__file__).resolve().parents[3]


def _seeder(tmp_path: Path, registry: SchemaRegistry):
    from dfe_engine.api.e2e.seed import Seed

    accounts, groups, *_ = bootstrap_auth(tmp_path / "config" / "auth")
    return Seed(
        account_store=accounts,
        group_store=groups,
        org_registry=OrgRegistry(tmp_path / "orgs"),
        schema_registry=registry,
    )


def test_searchable_schemas_stay_out_of_the_schema_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    snapshot = tmp_path / "schemas"
    snapshot.mkdir()
    kept = snapshot / "meta" / "syslog.yaml"
    kept.parent.mkdir()
    kept.write_text("current: '1.0.0'\n")
    fixtures = tmp_path / "schema-fixtures"
    registry = SchemaRegistry(schemas_directory=snapshot, fixtures_directory=fixtures)
    try:
        seeder = _seeder(tmp_path, registry)
        assert seeder.schemas.seed_searchable_meta_schemas() is True
        assert seeder.schemas.seed_searchable_meta_schemas() is False
        assert list(snapshot.rglob("searchable-*.yaml")) == []
        assert len(list(fixtures.rglob("searchable-*.yaml"))) == 50
        rows = [
            row
            for row in apply_search(registry.list_schemas(), "searchable", ["path"])
            if str(row.get("path", "")).strip("/").split("/")[0] == "meta"
        ]
        assert len(rows) == 50
        seeder.schemas.delete_all()
        assert list(fixtures.rglob("searchable-*.yaml")) == []
        assert kept.is_file()
    finally:
        registry.close()


def test_searchable_schemas_refuse_without_a_fixtures_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    snapshot = tmp_path / "schemas"
    registry = SchemaRegistry(schemas_directory=snapshot)
    try:
        seeder = _seeder(tmp_path, registry)
        with pytest.raises(RuntimeError, match="schema tree"):
            seeder.seed_static("seed_searchable_common_headers")
        assert list(snapshot.rglob("searchable-*.yaml")) == []
    finally:
        registry.close()


def test_searchable_schemas_refuse_the_schema_tree_and_the_worktree(tmp_path, monkeypatch):
    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    snapshot = tmp_path / "schemas"
    inside = SchemaRegistry(schemas_directory=snapshot, fixtures_directory=snapshot)
    worktree = SchemaRegistry(schemas_directory=snapshot, fixtures_directory=_REPO / "src")
    try:
        seeder = _seeder(tmp_path, inside)
        with pytest.raises(SchemaError, match="schema tree"):
            seeder.seed_static("seed_searchable_hunt_schemas")
        seeder = _seeder(tmp_path, worktree)
        with pytest.raises(SchemaError, match="worktree"):
            seeder.seed_static("seed_searchable_hunt_schemas")
        assert list(snapshot.rglob("searchable-*.yaml")) == []
        assert list((_REPO / "src").rglob("searchable-*.yaml")) == []
    finally:
        inside.close()
        worktree.close()
