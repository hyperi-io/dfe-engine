#  Purpose:      Seed the engine-owned source definitions shipped in dfe-schemas
#  License:      BUSL-1.1

"""The sources the engine owns rather than an operator.

One today: `main`, fronting the catch-all landing table. Its definition is
authored in dfe-schemas under ``sources/`` and reaches the deployment the way
every other schema resource does, through the image-baked seed tree that
``bootstrap.ensure_storage`` copies into the schemas directory.

The definition carries no ``source`` name. The engine fills it from
``clickhouse.landing_table``, the setting that also names the table the
ClickHouse bootstrap creates, so a deployment that renames its landing table
cannot end up with a source pointing at the old one.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from scalo.logger import logger

from dfe_engine.source.models import Source
from dfe_engine.yaml_utils import yaml_load

if TYPE_CHECKING:
    from dfe_engine.source.registry import SourceRegistry

SOURCES_SUBDIR = "sources"
"""Where dfe-schemas keeps its source definitions, under the schemas root."""

LANDING_SOURCE_FILE = "main.yaml"
"""The landing table's definition. Its stored name comes from the settings."""


def _drop_stale_core_sources(registry: SourceRegistry, settings: Any, *, keep: str) -> list[str]:
    """Remove every core source but *keep*, and the build records it leaves behind.

    A core source the settings no longer name fronts a table that is no longer the
    landing table. The ClickHouse table itself is left alone: reclaiming an
    operator's data is not this path's call.
    """
    dropped = []
    for name in registry.core_source_names():
        if name == keep:
            continue
        registry.delete_source(name, created_by="engine", core_reconcile=True)
        _forget_build_and_deploy(name=name, settings=settings)
        dropped.append(name)
        logger.info(f"Removed core source {name!r}: no longer the landing table")
    return dropped


def _forget_build_and_deploy(*, name: str, settings: Any) -> None:
    """Drop a removed source's build, plan and deploy records.

    Left behind, they outlive the source they describe, and the next source to take
    that name inherits them -- reporting itself deployed before it ever was.
    """
    from dfe_engine.source.deployment import SourceDeploymentStore

    try:
        SourceDeploymentStore.from_settings(settings).delete_source_records(name)
    except Exception as exc:
        logger.warning(
            f"core source {name!r} removed but its build records remain, so a later "
            f"source of that name will report itself already deployed: {exc}"
        )


def _landing_definition(settings: Any) -> Source | None:
    """The seeded landing definition with its name filled in, or None when absent.

    Absent is expected rather than exceptional: a deployment running an older
    dfe-schemas has no ``sources/`` tree, and the engine carries on without one.
    """
    schemas_dir = settings.schemas.schemas_dir
    if not (schemas_dir):
        return None
    path = Path(schemas_dir) / SOURCES_SUBDIR / LANDING_SOURCE_FILE
    if not (path.is_file()):
        return None

    table = settings.clickhouse.landing_table
    doc = yaml_load(path) or {}
    # resource_type is forced, not read: the whole point of this source is that no
    # API write path may touch it, and a shipped file that omitted the marker would
    # seed an operator-owned source with every gate silently inactive.
    return Source.model_validate(
        {**doc, "display_name": table, "resource_type": "core", "source": table}
    )


def _record_built_and_deployed(source: Source, settings: Any, *, tables_bootstrapped: bool) -> None:
    """Persist the build record, and the deploy record when the table really exists.

    Without them the console reports the landing source as never built and never
    deployed. The deploy record is withheld unless the ClickHouse bootstrap
    confirmed the table, because a deployment with ``bootstrap_tables`` off, or one
    whose ClickHouse was unreachable, has no such table to have deployed to.
    """
    from dfe_engine.schema.core_schema import landing_table_ddl
    from dfe_engine.source.deployment import (
        SchemaDeployResult,
        SourceDeploymentStore,
        ensure_build_artifact,
    )

    store = SourceDeploymentStore.from_settings(settings)
    _, artifact = ensure_build_artifact(
        store,
        source,
        refresh=True,
        schemas_base_dir=settings.schemas.schemas_dir,
        version_id=source.current,
    )
    if not (tables_bootstrapped):
        logger.info(
            f"core source {source.source!r} built but not recorded as deployed: the "
            "ClickHouse table bootstrap did not run"
        )
        return

    # The bootstrap's own DDL, not the build path's: the two render the landing
    # table differently, and the record has to say what was actually applied.
    result = SchemaDeployResult(
        applied=True,
        create_table=landing_table_ddl(settings),
        dry_run=False,
        source_name=source.source,
        statements_applied=1,
        version=source.current,
        views=dict(artifact.view_ddls),
    )
    store.save_deploy(result, source)


def seed_core_sources(
    registry: SourceRegistry, settings: Any, *, tables_bootstrapped: bool = False
) -> list[str]:
    """Adopt the seeded engine-owned definitions into the registry; return what changed.

    The shipped file is the truth on every start, so a definition updated by an
    engine upgrade reaches a deployment that already stored the old one. Nothing is
    written when the stored source already matches, so an unchanged start makes no
    gitcrud commit. Core means no operator edit is possible, which is what makes
    overwriting safe here and refused everywhere else.

    ``tables_bootstrapped`` says whether the ClickHouse bootstrap confirmed the
    core tables; without it the source is recorded as built but not as deployed.
    """
    definition = _landing_definition(settings)
    if definition is None:
        logger.info("No seeded source definitions found; skipping core source seeding")
        return []

    # Never claim a name an operator already holds: their source carries edits, a
    # transform and routing this definition does not, and adopting it would both
    # discard those and leave the result un-editable behind the core gate.
    if registry.source_exists(definition.source) and not (registry.is_core(definition.source)):
        logger.warning(
            f"Source {definition.source!r} already exists and is not engine-owned, so the "
            "landing definition was not seeded; rename it or the deployment's landing table"
        )
        return []

    stored = (
        registry.get_source(definition.source)
        if registry.source_exists(definition.source)
        else None
    )
    if stored is not None and stored.to_yaml_dict() == definition.to_yaml_dict():
        # The name is already right, so the sweep below can only find a rename's leftovers.
        return _drop_stale_core_sources(
            registry=registry, settings=settings, keep=definition.source
        )

    registry.save_source(
        definition,
        created_by="engine",
        description=f"source: seed core source {definition.source}",
        core_reconcile=True,
    )
    logger.info(f"Seeded core source {definition.source!r} for the landing table")

    # After the write, never before: a failed write must not leave the deployment
    # with no landing source at all until the next start.
    changed = _drop_stale_core_sources(registry=registry, settings=settings, keep=definition.source)
    changed.append(definition.source)

    try:
        _record_built_and_deployed(
            source=definition, settings=settings, tables_bootstrapped=tables_bootstrapped
        )
    except Exception as exc:
        logger.warning(
            f"core source {definition.source!r} seeded, but the console will report it as "
            f"never built: {exc}"
        )
    return changed
