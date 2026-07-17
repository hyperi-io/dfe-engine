"""Shared fixtures for schema tests."""

from pathlib import Path

import pytest

SCHEMAS_DIR = Path(__file__).resolve().parents[3] / "schemas"
# Probe a path the CURRENT dfe-schemas layout actually has, and the same one the
# code under test resolves (ddl_writer._resolve_hunt_results_path). The old probe
# was "hunt-results/detection.yaml", which the schemas repo no longer has anywhere
# -- so this gate reported "not checked out" for a submodule that WAS checked out,
# and every test below it skipped silently.
_has_schemas = (SCHEMAS_DIR / "hunts" / "results.yaml").exists()

requires_schemas = pytest.mark.skipif(
    not _has_schemas,
    reason="dfe-schemas submodule not checked out",
)
