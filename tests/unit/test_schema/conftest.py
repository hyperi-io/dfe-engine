"""Shared fixtures for schema tests."""

import pytest

from dfe_engine.schema.schema_loader import _resolve_package_schemas_root

# Probe a path the current dfe-schemas layout actually has, and the same one the
# code under test resolves (ddl_writer._resolve_hunt_results_path). A probe the
# package does not carry makes every test below skip silently.
_PACKAGE_ROOT = _resolve_package_schemas_root()
_has_schemas = _PACKAGE_ROOT is not None and (_PACKAGE_ROOT / "hunts" / "results.yaml").exists()

requires_schemas = pytest.mark.skipif(
    not _has_schemas,
    reason="dfe-schemas package not installed",
)
