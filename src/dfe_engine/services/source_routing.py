"""Source routing config generator.

Compiles Source definitions from SourceRegistry into receiver, loader and
archiver routing configuration.

The receiver emit targets the REAL dfe-receiver ``routing`` contract
(``src/config/mod.rs`` SourceRule/RoutingConfig): ``source_rules`` stamp
``_source`` (first match wins) and the topic derives as
``source_to_topic[_source]`` else ``{_source}{topic_suffix}``.

One source match drives BOTH receiver blocks. It compiles to the ``source_rules``
entry that labels the record, and on the direct transport to the
``destinations.rules`` entry that sends it to the stage that handles it - the
same field and value, read twice, so a source cannot be labelled one way and
routed another.

Usage:
    from dfe_engine.services.source_routing import compile_receiver_routing, compile_loader_routing

    receiver_routing = compile_receiver_routing(source_registry)
    loader_routing = compile_loader_routing(source_registry, db="prod")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scalo.logger import logger

from dfe_engine.services.models.loader import LoaderRoutingConfig
from dfe_engine.services.models.receiver import (
    BUS_DESTINATION,
    LOADER_DESTINATION,
    DestinationRef,
    DestinationRule,
    DestinationsConfig,
    ReceiverMatchMode,
    ReceiverRoutingConfig,
    SourceRule,
)
from dfe_engine.source.flow import (
    ARCHIVER_SERVICE,
    FlowError,
    archiver_endpoint,
    resolve_flow,
)
from dfe_engine.source.models import (
    DEFAULT_LANDING_LABEL,
    RULELESS_OPERATORS,
    SOURCE_LABEL_FIELD,
    TOPIC_LAND_SUFFIX,
    Source,
)
from dfe_engine.source.registry import SourceRegistry

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


def operator_mode(operator: str) -> ReceiverMatchMode | None:
    """The receiver router mode for a match operator, or None when it has none."""
    return _OPERATOR_TO_MODE.get(operator)


@dataclass(frozen=True, slots=True)
class ReceiverMatch:
    """What the receiver tests to recognise one source's records."""

    field: str
    mode: ReceiverMatchMode
    value: str | None
    """The operand, or None for a mode that only tests that the field is there."""


def receiver_match(source: Source) -> ReceiverMatch | None:
    """How the receiver recognises this source, or None when it needs no rule.

    The one reading of a source's match, so the rule that LABELS a record and the
    destination rule that SENDS it on cannot disagree about which records belong
    to the source.

    A fetcher-based source has no match rule of its own: its fetcher stamps
    ``_source`` with the source's landing label on every record, so the match is
    ``_source == <label>``. One landing on ``main`` needs no rule, because the
    receiver's ``default_source`` already sends an unmatched record there. An
    explicit per-source rule rather than ``key_value_use`` on ``_source``: the
    receiver takes untrusted input, and a use-the-value rule would let any
    sender pick any topic.
    """
    if source.match is None:
        label = source.landing_label()
        if source.fetcher is None or label == DEFAULT_LANDING_LABEL:
            return None
        return ReceiverMatch(field=SOURCE_LABEL_FIELD, mode="key_value_set", value=label)

    # The main flow needs no rule: default_source already sends an unmatched
    # record there, and a match-everything rule would shadow the rules after it.
    if source.match.operator in RULELESS_OPERATORS:
        return None

    mode = operator_mode(source.match.operator)
    if mode is None:
        logger.warning(
            f"Source {source.source!r}: match operator {source.match.operator!r} has no "
            f"receiver mode (documented receiver gap) - skipping this source in the "
            f"receiver routing compile"
        )
        return None

    return ReceiverMatch(
        field=source.match.field,
        mode=mode,
        value=source.match.value if mode == "key_value_set" else None,
    )


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
    topic_suffix: str = TOPIC_LAND_SUFFIX,
) -> ReceiverRoutingConfig:
    """Compile Source match rules into the receiver's ``routing`` contract.

    Iterates ACTIVE sources (a dormant source keeps its schema pre-positioned
    but its receiver redirect stays off), translating each ``match`` into a
    receiver ``SourceRule``:

    - ``equals`` -> ``key_value_set`` (match_value=value, source=_source name)
    - ``exists`` -> ``key_present``  (source=_source name)
    - ``always`` -> no rule at all; that is the main flow, and
      ``default_source`` already sends an unmatched record to it

    ``source_to_topic`` is emitted only where a source's landing topic
    deviates from ``{_source}{topic_suffix}``, which is the rule the receiver
    itself applies to the label it stamped - so the baseline is the landing
    LABEL, never the source name. A source landing on the shared topic is not a
    deviation: label and topic move together.

    A stored source whose match operator has no receiver mode is SKIPPED with
    a loud warning rather than raised on: the registry save path rejects new
    ones (see ``UnsupportedMatchOperatorError``), so this only fires for a
    legacy stored doc - and one legacy doc must not brick the whole receiver
    config compile.
    """
    rules: list[SourceRule] = []
    source_to_topic: dict[str, str] = {}

    for source in registry.get_all_sources(states=("active",)):
        match = receiver_match(source)
        if match is None:
            continue

        rules.append(
            SourceRule(
                field=match.field,
                mode=match.mode,
                match_value=match.value,
                source=source.landing_label(),
            )
        )
        expected_topic = f"{source.landing_label()}{topic_suffix}"
        if source.topic_land != expected_topic:
            source_to_topic[source.source] = source.topic_land

    return ReceiverRoutingConfig(
        source_rules=rules,
        default_source=default_source,
        topic_suffix=topic_suffix,
        source_to_topic=source_to_topic,
    )


def compile_receiver_destinations(registry: SourceRegistry, settings: Any) -> DestinationsConfig:
    """Compile where the receiver sends each matched record, on the direct transport.

    On the bus the receiver produces to a topic and the next stage consumes it,
    so there is nothing to name: the default stays the bus and no rule is
    emitted. On direct there is no store between stages, so every record has to
    be handed to a stage by address - its source's transform when it has one,
    else the loader.

    The loader is the exception that carries no address: while the destination
    set declares no ``loader`` entry, the receiver resolves its built-in one from
    its own ``loader`` block. That is ``loader.grpc_endpoint`` only once
    ``loader.transport`` is ``grpc``. Under its ``kafka`` default the name
    resolves to the bus. The deployment sets ``grpc`` on the direct transport,
    as the dfe-infra receiver chart does. Naming it here would take precedence
    over the endpoint the loader is deployed on, so it is referenced and left for
    the receiver to resolve.

    The default destination follows the DEPLOYMENT, not a source, because it is
    what an unmatched record takes. Unless the default flow itself is direct and
    carries a transform, in which case that transform IS the destination for
    everything unmatched, exactly as ``always`` compiles to ``default_source``
    rather than to a rule.

    An archived source names TWO: there is no landing topic for the archiver to
    read on direct, so the sender fans the record out to it beside the stage that
    handles it, and the raw copy is kept before any transform sees it.
    """
    default: DestinationRef = (
        LOADER_DESTINATION if settings.transport.default == "direct" else BUS_DESTINATION
    )
    rules: list[DestinationRule] = []
    endpoints: dict[str, str] = {}

    for source in registry.get_all_sources(states=("active",)):
        try:
            flow = resolve_flow(source, settings)
        except FlowError as exc:
            logger.warning(
                f"Source {source.source!r} cannot run on this deployment ({exc}) - skipping "
                f"it in the receiver destinations compile"
            )
            continue
        if flow.transport != "direct":
            continue

        if flow.transform is not None and flow.transform.endpoint:
            name = flow.transform.instance
            endpoints[name] = flow.transform.endpoint
        else:
            # Referenced by name and given no endpoint: an entry here would win
            # over the receiver's own loader.grpc_endpoint, which is the address
            # the loader is actually deployed on.
            name = LOADER_DESTINATION

        destination: DestinationRef = name
        if flow.outputs.archive:
            endpoints[ARCHIVER_SERVICE] = archiver_endpoint(settings)
            destination = [name, ARCHIVER_SERVICE]

        match = receiver_match(source)
        if match is None:
            # The default flow: everything unmatched already arrives here.
            if source.match is not None and source.match.operator in RULELESS_OPERATORS:
                default = destination
            continue
        if match.value is None:
            # A destination is picked on field AND value, so this source falls to
            # the default. The save path refuses it where it would skip a
            # transform; a stored one only reaches here after the deployment moved.
            logger.warning(
                f"Source {source.source!r}: its match tests no value, so on direct it takes "
                f"the default destination rather than one of its own"
            )
            continue
        rules.append(
            DestinationRule(
                match_field=match.field, match_value=match.value, destination=destination
            )
        )

    return DestinationsConfig(
        default=default,
        rules=rules,
        **{name: {"grpc": {"endpoint": endpoint}} for name, endpoint in sorted(endpoints.items())},
    )


def compile_archiver_topics(registry: SourceRegistry, settings: Any) -> list[str]:
    """The landing topics the archiver reads, from the sources that asked for it.

    On the bus the archiver keeps the raw record by consuming the landing topic,
    so archiving one source and not another is this list and nothing else. An
    archiver left to a discovery pattern instead keeps every source's records
    whether it was asked to or not, which is the drift this compile removes.

    Direct sources are absent by construction: nothing holds their records, so
    the receiver fans them out to the archiver's listener instead
    (``compile_receiver_destinations``). The bucket and the path an object lands
    under stay the deployment's, so only the topics are derived here.
    """
    # A set because several sources can land on the shared topic, and subscribing
    # to it once per source would consume every record that many times.
    topics: set[str] = set()
    for source in registry.get_all_sources(states=("active",)):
        if not source.archive:
            continue
        try:
            flow = resolve_flow(source, settings)
        except FlowError as exc:
            logger.warning(
                f"Source {source.source!r} cannot run on this deployment ({exc}) - skipping "
                f"it in the archiver topics compile"
            )
            continue
        if flow.transport == "bus":
            topics.add(source.topic_land)
    return sorted(topics)


def compile_loader_capture(
    registry: SourceRegistry,
    *,
    db: str,
    derived_base_dir: Any = None,
) -> dict[str, str]:
    """Compile each active source's derived-schema capture switches into loader modes.

    A source with no derived schema, or one whose derived schema keeps both
    switches on, contributes nothing: the loader's global ``capture_mode``
    already populates both columns.

    The key is the table the loader writes to, qualified the way the loader
    matches it (``<db>.<table>``), so the value the engine emits here is the one
    dfe-loader looks up.

    Args:
        registry: SourceRegistry to read Source definitions from.
        db: Default database the loader writes to.
        derived_base_dir: Root a ``derived/...`` reference resolves under
            (``DerivedSchemaRegistry.reference_root``).
    """
    from dfe_engine.schema.derived import CAPTURE_MODE_FULL, capture_mode
    from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2
    from dfe_engine.schema.schema_loader import SchemaLoader, SchemaLoadError

    builder = SchemaBuilderV2(derived_base_dir=derived_base_dir)
    modes: dict[str, str] = {}
    for source in registry.get_all_sources(states=("active",)):
        schema_cfg = source.version().effective_schema()
        if not schema_cfg.derived_schema:
            continue
        try:
            switches = SchemaLoader.load_derived_capture(
                builder.resolve_derived_path(schema_cfg.derived_schema),
                version=schema_cfg.derived_schema_version,
            )
        except SchemaLoadError as exc:
            logger.warning(
                f"Source {source.source!r} names derived schema "
                f"{schema_cfg.derived_schema!r}, which does not read ({exc}) - its "
                f"capture switches are skipped and the loader default applies"
            )
            continue
        mode = capture_mode(
            capture_json=switches["capture_json"], capture_raw=switches["capture_raw"]
        )
        if mode != CAPTURE_MODE_FULL:
            modes[f"{db}.{source.table_name}"] = mode
    return modes


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
