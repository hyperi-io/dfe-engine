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
_CANONICAL = _ROOT / "schemas" / "ddl" / "repository.sql"


def test_bundled_ddl_matches_canonical() -> None:
    """Bundled DDL must be byte-identical to the schemas submodule copy."""
    if not _CANONICAL.exists():
        pytest.skip("dfe-schemas submodule not checked out")

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
