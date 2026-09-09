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
    ReceiverMatchMode,
    ReceiverRoutingConfig,
    SourceRule,
)
from dfe_engine.source.models import DEFAULT_LANDING_LABEL
from dfe_engine.source.registry import SourceRegistry

# The top-level field dfe-fetcher stamps with the source's landing label.
FETCHER_LABEL_FIELD = "_source"

# SourceMatch.operator -> receiver SourceRule.mode. The receiver's hot-path
# router has exactly three modes; the other four engine operators (not_equals,
# includes, starts_with, ends_with) have NO receiver equivalent - a DOCUMENTED
# receiver gap. The registry save path rejects them for non-disabled sources
# (see UnsupportedMatchOperatorError); compile skips a stored legacy one with
# a loud warning instead of failing the whole receiver config.
_OPERATOR_TO_MODE: dict[str, ReceiverMatchMode] = {
    "equals": "key_value_set",
    "exists": "key_present",
}

RULELESS_OPERATORS = frozenset({"always"})
"""Operators the receiver honours WITHOUT a rule.

``always`` is the default flow: an unmatched record already goes to
``default_source``, so emitting a rule that matches everything would shadow every
rule after it. The registry save path allows it only on the reserved ``default``
source.
"""


def operator_mode(operator: str) -> ReceiverMatchMode | None:
    """The receiver router mode for a match operator, or None when it has none."""
    return _OPERATOR_TO_MODE.get(operator)


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
    default_source: str = DEFAULT_LANDING_LABEL,
    topic_suffix: str = "_land",
) -> ReceiverRoutingConfig:
    """Compile Source match rules into the receiver's ``routing`` contract.

    Iterates ACTIVE sources (a dormant source keeps its schema pre-positioned
    but its receiver redirect stays off), translating each ``match`` into a
    receiver ``SourceRule``:

    - ``equals`` -> ``key_value_set`` (match_value=value, source=_source name)
    - ``exists`` -> ``key_present``  (source=_source name)
    - ``always`` -> no rule at all; that is the default flow, and
      ``default_source`` already sends an unmatched record to it

    A fetcher-based source has no match rule of its own: its fetcher stamps
    ``_source`` with the source's landing label on every record, so the rule
    compiled for it is ``_source == <label>`` (key_value_set). One landing on
    the platform default table needs no rule, because the receiver's
    ``default_source`` already sends an unmatched record there. An explicit
    per-source rule rather than ``key_value_use`` on ``_source``: the receiver
    takes untrusted input, and a use-the-value rule would let any sender pick
    any topic.
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
            label = source.landing_label()
            if source.fetcher is not None and label != DEFAULT_LANDING_LABEL:
                rules.append(
                    SourceRule(
                        field=FETCHER_LABEL_FIELD,
                        mode="key_value_set",
                        match_value=label,
                        source=label,
                    )
                )
            continue

        # The default flow needs no rule: default_source already sends an
        # unmatched record there, and a match-everything rule would shadow the
        # rules after it.
        if source.match.operator in RULELESS_OPERATORS:
            continue

        mode = operator_mode(source.match.operator)
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
    db: str = "dfe",
) -> LoaderRoutingConfig:
    """Compile Source definitions into a LoaderRoutingConfig.

    The loader takes the table from the first ``table_fields`` hit, and the
    default ``["_source"]`` is exactly the field the receiver stamps - so a
    source lands in a table of its own name with no map at all. What the map
    is FOR is the source whose table_name differs from its source name, and
    emitting it for every ACTIVE source keeps the two in step without the
    compile having to know which ones diverge.

    Dormant and disabled sources are left out: their schema stays
    pre-positioned but they have no live loader path.

    Args:
        registry: SourceRegistry to read Source definitions from.
        db: Default database for the loader.
    """
    source_to_table: dict[str, str] = {}
    for source in registry.get_all_sources(states=("active",)):
        source_to_table[source.source] = source.table_name

    return LoaderRoutingConfig(
        default_db=db,
        source_to_table=source_to_table,
    )
