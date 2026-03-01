"""Source routing config generator.

Compiles Source definitions from SourceRegistry into receiver/loader
routing configuration for source_routing mode.

Usage:
    from dfe_engine.services.source_routing import compile_receiver_routing, compile_loader_routing

    receiver_routing = compile_receiver_routing(source_registry)
    loader_routing = compile_loader_routing(source_registry, db="prod")
"""

from __future__ import annotations

from dfe_engine.services.models.loader import LoaderRoutingConfig
from dfe_engine.services.models.receiver import (
    ReceiverRoutingConfig,
    SourceMatchRule,
)
from dfe_engine.source.registry import SourceRegistry


def compile_receiver_routing(
    registry: SourceRegistry,
    *,
    default_topic: str = "unmatched",
) -> ReceiverRoutingConfig:
    """Compile Source match rules into a ReceiverRoutingConfig.

    Iterates all enabled Sources, extracts their ``match`` config,
    and builds a ``source_match_table`` for the receiver's source_routing mode.

    Sources without a ``match`` config are skipped — they won't be
    directly matched by the receiver (e.g. fetcher-based SaaS sources).

    Args:
        registry: SourceRegistry to read Source definitions from.
        default_topic: Fallback topic for unmatched messages.

    Returns:
        ReceiverRoutingConfig with source_routing=True and populated match table.
    """
    match_table: list[SourceMatchRule] = []

    for source in registry.get_all_sources(enabled_only=True):
        if not source.match:
            continue

        match_table.append(
            SourceMatchRule(
                field=source.match.field,
                value=source.match.value,
                topic=source.topic_land,
            )
        )

    return ReceiverRoutingConfig(
        source_routing=True,
        source_match_table=match_table,
        default_topic=default_topic,
    )


def compile_loader_routing(
    registry: SourceRegistry,
    *,
    db: str = "common",
    source_field: str = "_source",
) -> LoaderRoutingConfig:
    """Compile Source definitions into a LoaderRoutingConfig.

    In source_routing mode, the loader reads the ``_source`` field from
    each message and uses it directly as the ClickHouse table name.
    No category_to_table mapping is needed.

    The ``category_to_table`` map is still populated (from Source definitions)
    as a diagnostic aid — it shows which sources map to which tables.

    Args:
        registry: SourceRegistry to read Source definitions from.
        db: Default database for the loader.
        source_field: JSON field containing the source name.

    Returns:
        LoaderRoutingConfig with source_routing=True.
    """
    # Build a diagnostic map of source → table_name
    source_to_table: dict[str, str] = {}
    for source in registry.get_all_sources(enabled_only=True):
        source_to_table[source.source] = source.table_name

    return LoaderRoutingConfig(
        source_routing=True,
        source_field=source_field,
        default_db=db,
        category_to_table=source_to_table,
    )
