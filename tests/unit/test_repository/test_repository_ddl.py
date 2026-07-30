#  Project:      dfe-engine
#  File:         test_repository_ddl.py
#  Purpose:      Guards for the bundled repository DDL (sync + invariants)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Guard: bundled repository DDL must match the dfe-schemas submodule copy.

The bundled copy (``src/dfe_engine/repository/resources/repository.sql``)
is the fallback the engine auto-creates from when the ``schemas``
submodule is absent. It must never hand-drift from canonical
``schemas/ddl/repository.sql``.

Regenerate with::

    cp schemas/ddl/repository.sql src/dfe_engine/repository/resources/repository.sql
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_BUNDLED = _ROOT / "src" / "dfe_engine" / "repository" / "resources" / "repository.sql"
_SUBMODULE = _ROOT / "schemas"
_CANONICAL = _SUBMODULE / "ddl" / "repository.sql"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "dfe-schemas has no ddl/ directory, so schemas/ddl/repository.sql does not exist "
        "even with the submodule checked out (CI pins `submodules: schemas`), and the drift "
        "guard below has nothing to compare against. Either dfe-schemas publishes the "
        "canonical DDL under ddl/, or the bundled copy is declared the source of truth and "
        "this guard removed. Remove this marker once the canonical file exists."
    ),
)
def test_canonical_ddl_exists_in_schemas_submodule() -> None:
    """The canonical DDL must exist whenever the submodule is checked out.

    Unguarded on purpose: an absent canonical DDL is the failure mode this test
    exists to report, so guarding on it would leave the test incapable of
    failing. The submodule-absent case belongs to
    test_bundled_ddl_matches_canonical.
    """
    assert _SUBMODULE.is_dir(), f"schemas submodule missing entirely: {_SUBMODULE}"
    assert _CANONICAL.exists(), f"canonical DDL not found: {_CANONICAL}"


def test_bundled_ddl_matches_canonical() -> None:
    """Bundled DDL must be byte-identical to the schemas submodule copy."""
    if not _CANONICAL.exists():
        # An unchecked-out submodule is a legitimate local-only skip; a checked-out
        # submodule missing the file is a defect, pinned by
        # test_canonical_ddl_exists_in_schemas_submodule above.
        if not _SUBMODULE.is_dir() or not any(_SUBMODULE.iterdir()):
            pytest.skip("dfe-schemas submodule not checked out")
        pytest.skip(
            f"{_CANONICAL.relative_to(_ROOT)} absent from the checked-out dfe-schemas "
            "submodule -- see test_canonical_ddl_exists_in_schemas_submodule"
        )

    assert _BUNDLED.read_text(encoding="utf-8") == _CANONICAL.read_text(encoding="utf-8"), (
        "Bundled repository DDL drifted from schemas/ddl/repository.sql. "
        "Regenerate: cp schemas/ddl/repository.sql "
        "src/dfe_engine/repository/resources/repository.sql"
    )


def _ddl_without_comments() -> str:
    """DDL statements only - the header comment mentions _org_id by design."""
    lines = _BUNDLED.read_text(encoding="utf-8").splitlines()
    return "\n".join(ln for ln in lines if not ln.strip().startswith("--"))


def test_ddl_has_no_org_id_column() -> None:
    """No _org_id by design - keeps ChRbacReconciler _org_id discovery away."""
    assert "_org_id" not in _ddl_without_comments()


def test_ddl_has_no_partition_by() -> None:
    """No PARTITION BY - ReplacingMergeTree dedup must stay within one part tree."""
    assert "PARTITION BY" not in _BUNDLED.read_text(encoding="utf-8").upper()


def test_ddl_engine_and_key() -> None:
    """Dedup invariants: ReplacingMergeTree(updated_at, is_deleted) + full scope key."""
    ddl = _BUNDLED.read_text(encoding="utf-8")
    assert "ReplacingMergeTree(updated_at, is_deleted)" in ddl
    assert "ORDER BY (scope, scope_id, namespace, key)" in ddl
