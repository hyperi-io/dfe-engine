#  Project:      dfe-engine
#  File:         api/e2e/seed/schemas.py
#  Purpose:      Meta-schema seeder primitives for e2e-server
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Schema seeding. Public methods are the scripts. Privates are reusable.

The schema list searches the path and filters on its top-level directory, so
each script writes paths under ``meta``, ``common-header`` or ``hunts``. The
files themselves go to the fixtures directory, never into the schema tree, a
git worktree, or another environment's data. A reset removes only those paths.
"""

from __future__ import annotations

from dfe_engine.api.e2e.seed.base import Seed
from dfe_engine.schema.models import MetaSchema, SchemaColumn, SchemaVersion

_VERSION = "1.0.0"
_META = "meta"
_COMMON_HEADER = "common-header"
_HUNTS = "hunts"


def searchable_schema_paths(directory: str) -> list[str]:
    """Schema paths one searchable script writes under *directory*."""
    return [f"{directory}/searchable-{num}" for num in range(50)]


def _schema(path: str) -> MetaSchema:
    """One custom schema with a single column, enough for the list to show it."""
    return MetaSchema(
        path=path,
        current=_VERSION,
        versions={
            _VERSION: SchemaVersion(
                date="2026-01-01",
                type="model",
                summary="Searchable schema",
                columns=[SchemaColumn(name="event_id", type="string")],
            )
        },
    )


class Schemas(Seed):
    """Create or remove the searchable schemas."""

    def seed_searchable_meta_schemas(self) -> bool:
        """Seed fifty meta schemas a list search can filter.

        Returns True when any schema was written, False when they were all already there.
        """
        return self._seed(_META)

    def seed_searchable_common_headers(self) -> bool:
        """Seed fifty common-header schemas a list search can filter.

        Returns True when any schema was written, False when they were all already there.
        """
        return self._seed(_COMMON_HEADER)

    def seed_searchable_hunt_schemas(self) -> bool:
        """Seed fifty hunt schemas a list search can filter.

        Returns True when any schema was written, False when they were all already there.
        """
        return self._seed(_HUNTS)

    def delete_all(self) -> None:
        """Remove the fixture schemas this seeder writes. A no-op when no schema registry is configured.

        Only fixture files go. A schema in the tree with the same path stays.
        """
        registry = self._schema_registry
        if registry is None:
            return
        for directory in (_META, _COMMON_HEADER, _HUNTS):
            for path in searchable_schema_paths(directory):
                registry.delete_fixture(path)

    def _seed(self, directory: str) -> bool:
        """Write the fifty schemas under *directory*. Returns whether any were new.

        Writes only through the fixtures directory. With none configured the
        call refuses, rather than falling through to the schema tree.
        """
        registry = self._require_schema_registry()
        if registry.fixtures_directory is None:
            raise RuntimeError(
                "this e2e seed writes schema fixtures only outside the schema tree; "
                "set DFE_ENGINE_E2E_SCHEMA_FIXTURES_DIR"
            )
        changed = False
        for path in searchable_schema_paths(directory):
            if registry.find_schema_at_location(path) is not None:
                continue
            registry.save_fixture(_schema(path))
            changed = True
        return changed
