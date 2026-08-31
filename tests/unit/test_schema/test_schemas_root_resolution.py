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
    _resolve_package_schemas_root,
    _resolve_schemas_root,
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


def test_a_resolvable_root_missing_the_file_names_the_path(tmp_path):
    root = _make_schemas_tree(tmp_path / "schemas", hunts=False)
    (root / "hunts").mkdir()

    with pytest.raises(FileNotFoundError, match=r"detection_checkpoint\.yaml"):
        DDLFileWriter._resolve_hunt_detection_checkpoint_path(root)
