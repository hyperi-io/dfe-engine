"""Source routing config generator.

Compiles Source definitions from SourceRegistry into receiver/loader
routing configuration.

The receiver emit targets the REAL dfe-receiver ``routing`` contract
(``src/config/mod.rs`` SourceRule/RoutingConfig): ``source_rules`` stamp
``_source`` (first match wins) and the topic derives as
``source_to_topic[_source]`` else ``{_source}{topic_suffix}``.

Usage:
    from dfe_engine.services.source_routing import compile_receiver_routing, compile_loader_routing

    receiver_routing = compile_receiver_routing(source_registry)
    loader_routing = compile_loader_routing(source_registry, db="prod")
"""

from __future__ import annotations

from scalo.logger import logger

from dfe_engine.services.models.loader import LoaderRoutingConfig
from dfe_engine.services.models.receiver import (
    ReceiverRoutingConfig,
    SourceRule,
)
from dfe_engine.source.registry import SourceRegistry

# SourceMatch.operator -> receiver SourceRule.mode. The receiver's hot-path
# router has exactly three modes; the other four engine operators (not_equals,
# includes, starts_with, ends_with) have NO receiver equivalent - a DOCUMENTED
# receiver gap. The registry save path rejects them for non-disabled sources
# (see UnsupportedMatchOperatorError); compile skips a stored legacy one with
# a loud warning instead of failing the whole receiver config.
_OPERATOR_TO_MODE = {
    "equals": "key_value_set",
    "exists": "key_present",
}


class UnsupportedMatchOperatorError(ValueError):
    """A receiver-routed source uses a match operator the receiver cannot evaluate."""

    def __init__(self, source: str, operator: str) -> None:
        super().__init__(
            f"Source {source!r}: match operator {operator!r} has no dfe-receiver "
            f"equivalent (hot-path router modes: key_present, key_value_set, "
            f"key_value_use). Use 'equals' or 'exists', or route this source "
            f"another way - the missing operators are a documented receiver gap."
        )
        self.source = source
        self.operator = operator


def compile_receiver_routing(
    registry: SourceRegistry,
    *,
    default_source: str = "default",
    topic_suffix: str = "_land",
) -> ReceiverRoutingConfig:
    """Compile Source match rules into the receiver's ``routing`` contract.

    Iterates ACTIVE sources (a dormant source keeps its schema pre-positioned
    but its receiver redirect stays off), translating each ``match`` into a
    receiver ``SourceRule``:

    - ``equals`` -> ``key_value_set`` (match_value=value, source=_source name)
    - ``exists`` -> ``key_present``  (source=_source name)

    Sources without a ``match`` config are skipped — they won't be
    directly matched by the receiver (e.g. fetcher-based SaaS sources).
    ``source_to_topic`` is emitted only where a source's landing topic
    deviates from ``{_source}{topic_suffix}`` (none do today - the Source
    model derives ``topic_land`` by that same rule).

    A stored source whose match operator has no receiver mode is SKIPPED with
    a loud warning rather than raised on: the registry save path rejects new
    ones (see ``UnsupportedMatchOperatorError``), so this only fires for a
    legacy stored doc - and one legacy doc must not brick the whole receiver
    config compile.
    """
    rules: list[SourceRule] = []
    source_to_topic: dict[str, str] = {}

    for source in registry.get_all_sources(states=("active",)):
        if not source.match:
            continue

        mode = _OPERATOR_TO_MODE.get(source.match.operator)
        if mode is None:
            logger.warning(
                f"Source {source.source!r}: match operator {source.match.operator!r} has no "
                f"receiver mode (documented receiver gap) - skipping this source in the "
                f"receiver routing compile"
            )
            continue

        rules.append(
            SourceRule(
                field=source.match.field,
                mode=mode,
                match_value=source.match.value if mode == "key_value_set" else None,
                source=source.source,
            )
        )
        expected_topic = f"{source.source}{topic_suffix}"
        if source.topic_land != expected_topic:
            source_to_topic[source.source] = source.topic_land

    return ReceiverRoutingConfig(
        source_rules=rules,
        default_source=default_source,
        topic_suffix=topic_suffix,
        source_to_topic=source_to_topic,
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
    # Build a diagnostic map of source → table_name (ACTIVE only: dormant
    # sources have no live loader path).
    source_to_table: dict[str, str] = {}
    for source in registry.get_all_sources(states=("active",)):
        source_to_table[source.source] = source.table_name

    return LoaderRoutingConfig(
        source_routing=True,
        source_field=source_field,
        default_db=db,
        category_to_table=source_to_table,
    )
