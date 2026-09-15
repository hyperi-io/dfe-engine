#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_schemas_root_resolution.py
#  Purpose:      Pin schemas-root resolution, incl. the image seed a Job needs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every process in the image must be able to read the schemas.

The container ships them at ``/app/schemas-seed`` and only the daemon runs the
bootstrap that copies them into the runtime schemas dir. ``dfe-schema`` in a k8s
Job runs neither, so it resolved no schemas root and then built a path from
None -- a TypeError that said nothing about what was missing.
"""

from __future__ import annotations

import pytest

from dfe_engine.schema.ddl_writer import DDLFileWriter
from dfe_engine.schema.schema_loader import (
    SEED_DIR_ENV_VAR,
    SchemaLoadError,
    _resolve_package_schemas_root,
    _resolve_schemas_root,
    resolve_registry_path,
)


def _make_schemas_tree(root, *, hunts: bool = True):
    """A directory shaped enough to pass the common-header test."""
    (root / "common-header").mkdir(parents=True)
    if hunts:
        (root / "hunts").mkdir(parents=True)
        (root / "hunts" / "results.yaml").write_text("columns: []\n", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Neutralise the dfe-schemas package this test suite runs against."""
    monkeypatch.delenv("DFE_SCHEMAS_DIR", raising=False)
    monkeypatch.setenv(SEED_DIR_ENV_VAR, str(tmp_path / "no-seed-here"))
    monkeypatch.setattr(
        "dfe_engine.schema.schema_loader._resolve_package_schemas_root", lambda: None
    )


def test_the_real_package_maps_to_its_data_directory():
    """The wheel's ``dfe_schemas/data`` is what the loader treats as a schemas root.

    Called through the module-level import so the autouse fixture's patch of the
    module attribute does not shadow it.
    """
    root = _resolve_package_schemas_root()
    assert root is not None, "dfe-schemas is a declared dependency and must be installed"
    assert root.name == "data"
    assert root.parent.name == "dfe_schemas"
    for tree in ("common-header", "hunts", "meta", "tables", "additional"):
        assert (root / tree).is_dir(), f"{tree} missing under {root}"


def test_the_installed_package_beats_the_image_seed(monkeypatch, tmp_path):
    """The wheel is the schema source; the seed only covers a process that has none."""
    packaged = _make_schemas_tree(tmp_path / "site-packages" / "dfe_schemas" / "data")
    seed = _make_schemas_tree(tmp_path / "schemas-seed")
    monkeypatch.setenv(SEED_DIR_ENV_VAR, str(seed))
    monkeypatch.setattr(
        "dfe_engine.schema.schema_loader._resolve_package_schemas_root", lambda: packaged
    )

    assert _resolve_schemas_root() == packaged


def test_an_explicit_dir_wins_over_the_package(monkeypatch, tmp_path):
    """DFE_SCHEMAS_DIR is how a deployment overrides the shipped trees."""
    explicit = _make_schemas_tree(tmp_path / "explicit")
    packaged = _make_schemas_tree(tmp_path / "site-packages" / "dfe_schemas" / "data")
    monkeypatch.setenv("DFE_SCHEMAS_DIR", str(explicit))
    monkeypatch.setattr(
        "dfe_engine.schema.schema_loader._resolve_package_schemas_root", lambda: packaged
    )

    assert _resolve_schemas_root() == explicit


def test_the_image_seed_resolves_when_nothing_else_does(monkeypatch, tmp_path):
    """The regression: a Job has no env var, no checkout, only the seed."""
    seed = _make_schemas_tree(tmp_path / "schemas-seed")
    monkeypatch.setenv(SEED_DIR_ENV_VAR, str(seed))

    assert _resolve_schemas_root() == seed


def test_an_empty_dir_never_shadows_a_real_tree(monkeypatch, tmp_path):
    """`/app/schemas` exists but is empty until the daemon seeds it."""
    empty = tmp_path / "schemas"
    empty.mkdir()
    seed = _make_schemas_tree(tmp_path / "schemas-seed")
    monkeypatch.setenv("DFE_SCHEMAS_DIR", str(empty))
    monkeypatch.setenv(SEED_DIR_ENV_VAR, str(seed))

    assert _resolve_schemas_root() == seed


def test_an_explicit_dir_wins_over_the_seed(monkeypatch, tmp_path):
    """A real checkout must beat whatever the image happens to carry."""
    explicit = _make_schemas_tree(tmp_path / "explicit")
    seed = _make_schemas_tree(tmp_path / "schemas-seed")
    monkeypatch.setenv("DFE_SCHEMAS_DIR", str(explicit))
    monkeypatch.setenv(SEED_DIR_ENV_VAR, str(seed))

    assert _resolve_schemas_root() == explicit


def test_nothing_resolvable_returns_none():
    assert _resolve_schemas_root() is None


def test_an_unresolved_root_says_so_instead_of_a_type_error():
    """What the Job actually hit: `NoneType / str` told nobody anything."""
    with pytest.raises(FileNotFoundError, match="no dfe-schemas tree resolved"):
        DDLFileWriter._resolve_hunt_results_path()


def test_the_real_package_ships_every_registry():
    root = _resolve_package_schemas_root()

    assert root is not None
    for entry in ("engines.yaml", "types.yaml", "field-maps"):
        assert (root / "registries" / entry).exists(), f"registries/{entry} missing under {root}"


def _package_root_is(*, monkeypatch, root):
    def packaged_root():
        return root

    monkeypatch.setattr(
        "dfe_engine.schema.schema_loader._resolve_package_schemas_root", packaged_root
    )


def _write_registry_file(*, relative: str, root, text: str = "engines: []\n"):
    path = root / "registries" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_registry_in_the_explicit_dir_wins(monkeypatch, tmp_path):
    explicit = _write_registry_file(relative="engines.yaml", root=tmp_path / "explicit")
    _write_registry_file(relative="engines.yaml", root=tmp_path / "packaged")
    monkeypatch.setenv("DFE_SCHEMAS_DIR", str(tmp_path / "explicit"))
    _package_root_is(monkeypatch=monkeypatch, root=tmp_path / "packaged")

    assert resolve_registry_path("engines.yaml") == explicit


def test_a_volume_without_the_registry_does_not_shadow_the_package(monkeypatch, tmp_path):
    """A volume seeded by an engine that predates registries/ still has common-header."""
    stale = _make_schemas_tree(tmp_path / "stale-volume")
    packaged = _write_registry_file(
        relative="types.yaml", root=tmp_path / "packaged", text="primitives: {}\n"
    )
    monkeypatch.setenv("DFE_SCHEMAS_DIR", str(stale))
    _package_root_is(monkeypatch=monkeypatch, root=tmp_path / "packaged")

    assert resolve_registry_path("types.yaml") == packaged


def test_an_empty_registry_dir_on_the_volume_does_not_shadow_the_package(monkeypatch, tmp_path):
    (tmp_path / "volume" / "registries" / "field-maps" / "sigma").mkdir(parents=True)
    packaged = _write_registry_file(
        relative="field-maps/sigma/_default.yaml", root=tmp_path / "packaged", text="{}\n"
    )
    monkeypatch.setenv("DFE_SCHEMAS_DIR", str(tmp_path / "volume"))
    _package_root_is(monkeypatch=monkeypatch, root=tmp_path / "packaged")

    assert resolve_registry_path("field-maps") == packaged.parent.parent


def test_a_missing_registry_names_what_was_searched(monkeypatch, tmp_path):
    monkeypatch.setenv("DFE_SCHEMAS_DIR", str(tmp_path / "explicit"))

    with pytest.raises(SchemaLoadError, match=r"registries/engines\.yaml.*explicit"):
        resolve_registry_path("engines.yaml")


def test_a_resolvable_root_missing_the_file_names_the_path(tmp_path):
    root = _make_schemas_tree(tmp_path / "schemas", hunts=False)
    (root / "hunts").mkdir()

    with pytest.raises(FileNotFoundError, match=r"detection_checkpoint\.yaml"):
        DDLFileWriter._resolve_hunt_detection_checkpoint_path(root)
