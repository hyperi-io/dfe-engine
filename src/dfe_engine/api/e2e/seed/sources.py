#  Project:      dfe-engine
#  File:         api/e2e/seed/sources.py
#  Purpose:      Source-definition seeder primitives for e2e-server
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Source seeding. Public methods are the scripts; privates are reusable."""

from __future__ import annotations

from dfe_engine.api.e2e.seed.base import Seed
from dfe_engine.source.models import (
    SourceFetcher,
    SourceHeader,
    SourceMatch,
    SourceSchema,
    SourceWriteRequest,
)
from dfe_engine.source.registry import SourceNotFoundError

SEED_SOURCE_NAME = "seedsource"
"""The source every app-management seed binds to; siblings key their instances off it.

Deliberately hyphen-free as well as underscore-free: a hyphen is legal in a source
name but not yet quoted in a ClickHouse identifier position, so a hyphenated fixture
would break the moment a spec asked the API to render this source's DDL.
"""

SEED_ACTOR = "e2e-seed"
"""Commit attribution for every deploy-repo write a seeder makes."""

_MATCH_FIELD = "_json.tags.collector.type"
_FETCHER_TYPE = "http_json"
_FETCHER_URL = "https://fetcher.invalid/events"


class Sources(Seed):
    """Create or reset source definitions in the source registry."""

    def seed_source(self, name: str = SEED_SOURCE_NAME) -> bool:
        """Seed a source with the match rule the receiver routing compiles from.

        Returns True when created, False when it was already defined.
        """
        return self._ensure_source(name)

    def delete_all(self) -> None:
        """Clear every source definition. A no-op when no registry is configured."""
        registry = self._source_registry
        if registry is None:
            return
        for entry in registry.list_sources():
            registry.delete_source(str(entry["source"]), created_by=SEED_ACTOR)

    def _ensure_source(self, name: str) -> bool:
        """Create the source when absent. Existing definitions are left alone.

        A source carries a version tree, and rewriting one would bump versions on
        every call, so this is create-once rather than the reset the account and
        organisation seeders do.
        """
        registry = self._require_source_registry()
        try:
            registry.get_source(name)
        except SourceNotFoundError:
            pass
        else:
            return False
        registry.create_source_from_write(
            SourceWriteRequest(
                source=name,
                display_name="Seed Source",
                description="Playwright fixture source.",
                match=SourceMatch(field=_MATCH_FIELD, operator="equals", value=name),
                header=SourceHeader(),
                schema_config=SourceSchema(),
                fetcher=SourceFetcher(
                    source_type=_FETCHER_TYPE,
                    base_url=_FETCHER_URL,
                    poll_interval_secs=300,
                ),
            ),
            created_by=SEED_ACTOR,
            description=f"e2e: seed source {name}",
        )
        return True
