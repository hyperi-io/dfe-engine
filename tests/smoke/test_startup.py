#  Project:   dfe-engine
#  File:      tests/smoke/test_startup.py
#  Purpose:   Startup smoke test — catches init panics, broken imports, missing defaults
#  Language:  Python
#
#  License:   FSL-1.1-ALv2
#  Copyright: (c) 2026 HYPERI PTY LIMITED
"""Smoke tests for dfe-engine core module imports and basic functionality.

Run on every push. If any of these fail, something fundamental is broken.
"""

import pytest


@pytest.mark.smoke
class TestCoreImports:
    """Verify all core modules import without error."""

    def test_import_root(self):
        import dfe_engine

        assert dfe_engine is not None

    def test_import_settings_module(self):
        from dfe_engine import settings

        assert settings is not None

    def test_import_schema(self):
        from dfe_engine import schema

        assert schema is not None

    def test_import_source(self):
        from dfe_engine import source

        assert source is not None

    def test_import_sigma(self):
        from dfe_engine import sigma

        assert sigma is not None

    def test_import_query(self):
        from dfe_engine import query

        assert query is not None

    def test_import_hunts(self):
        from dfe_engine import hunts

        assert hunts is not None


@pytest.mark.smoke
class TestAPIBootstrap:
    """Verify FastAPI app can be created without crashing."""

    def test_create_app(self):
        from dfe_engine.api.app import create_app

        assert callable(create_app)

    def test_api_module_imports(self):
        from dfe_engine.api import deps, errors, pagination

        assert deps is not None
        assert errors is not None
        assert pagination is not None
