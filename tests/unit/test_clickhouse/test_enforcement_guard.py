#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_enforcement_guard.py
#  Purpose:      Guard - the canonical ClickHouse object is the ONLY way to touch CH
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Enforcement guard for the "everything routes through clickhouse/" rule.

Fails on the two ways the rule rots, so it cannot be silently undone:
  1. a direct ``clickhouse_connect`` import OUTSIDE the clickhouse/ package - every
     other caller uses the ClickHouseManager wrapper or ``get_pooled_client``;
  2. a hardcoded table-engine literal (``ENGINE = ...MergeTree(...)``) in a .py file
     - every create must resolve its engine via ``clickhouse/engines.py`` so it gets
     the right form per topology (single / cluster / Cloud), never a fixed string.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src" / "dfe_engine"
_CH_PKG = _SRC / "clickhouse"

# Real import statements only (a comment/docstring mention does not start this way).
_IMPORT_RE = re.compile(r"^\s*(?:import\s+clickhouse_connect|from\s+clickhouse_connect\b)")

# A hardcoded MergeTree-family engine literal spliced into DDL. The wired sites use
# ``ENGINE = {resolved.clause}`` (an f-string hole), which does NOT match.
_ENGINE_RE = re.compile(
    r"ENGINE\s*=\s*['\"]?"
    r"(?:Replicated|Shared)?"
    r"(?:Replacing|Summing|Aggregating|Collapsing|VersionedCollapsing|Graphite)?"
    r"MergeTree\s*\("
)


def _py_files() -> list[Path]:
    return [p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_direct_clickhouse_connect_import_outside_the_object() -> None:
    """clickhouse_connect may be imported ONLY inside the clickhouse/ package."""
    violations: list[str] = []
    for path in _py_files():
        if _CH_PKG in path.parents:
            continue  # the object itself owns the driver
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if _IMPORT_RE.match(line):
                violations.append(f"{path.relative_to(_SRC)}:{lineno}: {line.strip()}")
    assert not violations, (
        "Direct clickhouse_connect import outside clickhouse/ - route through the "
        "ClickHouseManager wrapper or clickhouse.clickhouse_manager.get_pooled_client:\n"
        + "\n".join(violations)
    )


def test_no_hardcoded_table_engine_outside_the_object() -> None:
    """Every table create resolves its engine via clickhouse/engines.py, not a literal."""
    violations: list[str] = []
    for path in _py_files():
        if _CH_PKG in path.parents:
            continue  # engines.py + the DAL own the engine strings
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue  # a comment mentioning an engine is not a real create
            if _ENGINE_RE.search(line):
                violations.append(f"{path.relative_to(_SRC)}:{lineno}: {line.strip()}")
    assert not violations, (
        "Hardcoded table-engine literal - resolve it via "
        "clickhouse.engines.EngineResolver and splice ``{resolved.clause}`` instead:\n"
        + "\n".join(violations)
    )
