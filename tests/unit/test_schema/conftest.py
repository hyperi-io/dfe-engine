"""Shared fixtures for schema tests."""

from pathlib import Path

import pytest

SCHEMAS_DIR = Path(__file__).resolve().parents[3] / "schemas"
_has_schemas = (SCHEMAS_DIR / "hunt-results" / "detection.yaml").exists()

requires_schemas = pytest.mark.skipif(
    not _has_schemas,
    reason="dfe-schemas submodule not checked out",
)
