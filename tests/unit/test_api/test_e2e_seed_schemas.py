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
        same_path_in_tree = snapshot / "meta" / "searchable-0.yaml"
        same_path_in_tree.write_text("current: '1.0.0'\n")
        seeder.schemas.delete_all()
        assert list(fixtures.rglob("searchable-*.yaml")) == []
        assert kept.is_file()
        assert same_path_in_tree.is_file()
    finally:
        registry.close()


@pytest.mark.parametrize(
    ("env", "e2e_server", "attached"),
    [
        ("test", True, True),
        ("dev", True, True),
        ("test", False, False),
        ("dev", False, False),
    ],
)
def test_fixture_schemas_are_visible_only_on_the_e2e_server(tmp_path, env, e2e_server, attached):
    from dfe_engine.api.deps import bootstrap_registries, get_schema_registry, shutdown_registries
    from dfe_engine.settings import DFESettings, SchemasSettings

    fixtures = tmp_path / "schema-fixtures"
    settings = DFESettings(
        env=env,
        e2e_server=e2e_server,
        schemas=SchemasSettings(
            schemas_dir=str(tmp_path / "schemas"), e2e_schema_fixtures_dir=str(fixtures)
        ),
    )
    bootstrap_registries(settings)
    try:
        expected = fixtures.resolve() if attached else None
        assert get_schema_registry().fixtures_directory == expected
    finally:
        shutdown_registries()


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
    for overlapping in (snapshot, snapshot / "fixtures", tmp_path):
        with pytest.raises(SchemaError, match="schema tree"):
            SchemaRegistry(schemas_directory=snapshot, fixtures_directory=overlapping)
    worktree = SchemaRegistry(schemas_directory=snapshot, fixtures_directory=_REPO / "src")
    try:
        seeder = _seeder(tmp_path, worktree)
        with pytest.raises(SchemaError, match="worktree"):
            seeder.seed_static("seed_searchable_hunt_schemas")
        assert list(snapshot.rglob("searchable-*.yaml")) == []
        assert list((_REPO / "src").rglob("searchable-*.yaml")) == []
    finally:
        worktree.close()
