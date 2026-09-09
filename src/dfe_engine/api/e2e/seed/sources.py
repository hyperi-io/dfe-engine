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

SEED_FETCHER_SOURCE_NAME = "seedfetch"
"""A fetcher-based source: the engine deploys a fetcher instance named for it."""

SEED_ACTOR = "e2e-seed"
"""Commit attribution for every deploy-repo write a seeder makes."""

# A bare dotted path: the receiver splits the field on '.' and walks the raw
# payload, so a '_json.' prefix is read as a first segment that no record has.
_MATCH_FIELD = "tags.collector.type"
# A public-registry family: it needs no credentials, so the stanza is inert data.
_FETCHER_TYPE = "crates_io"
_FETCHER_CONFIG = {"crates": ["dfe-fetcher"], "interval_secs": 3600}


class Sources(Seed):
    """Create or reset source definitions in the source registry."""

    def seed_source(self, name: str = SEED_SOURCE_NAME) -> bool:
        """Seed a receiver-based source with the match rule the routing compiles from.

        Returns True when created, False when it was already defined.
        """
        return self._ensure_source(name)

    def seed_fetcher_source(self, name: str = SEED_FETCHER_SOURCE_NAME) -> bool:
        """Seed a fetcher-based source, marked deployed so its instance is due.

        Returns True when created.
        """
        created = self._ensure_source(name, fetched=True)
        if created:
            # The reconcile deploys a fetcher instance only for a deployed source,
            # and no seed reaches ClickHouse, so the version is marked by hand.
            self._require_source_registry().set_deployed_version(
                name, "1.0.0", created_by=SEED_ACTOR, description=f"e2e: deploy {name}"
            )
        return created

    def delete_all(self) -> None:
        """Clear every source definition. A no-op when no registry is configured."""
        registry = self._source_registry
        if registry is None:
            return
        for entry in registry.list_sources():
            registry.delete_source(str(entry["source"]), created_by=SEED_ACTOR)

    def _ensure_source(self, name: str, *, fetched: bool = False) -> bool:
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
                match=None
                if fetched
                else SourceMatch(field=_MATCH_FIELD, operator="equals", value=name),
                fetcher=SourceFetcher(source_type=_FETCHER_TYPE, config=dict(_FETCHER_CONFIG))
                if fetched
                else None,
                header=SourceHeader(),
                schema_config=SourceSchema(),
            ),
            created_by=SEED_ACTOR,
            description=f"e2e: seed source {name}",
        )
        return True
