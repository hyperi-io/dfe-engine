"""Source routing config generator.

Compiles Source definitions from SourceRegistry into receiver, loader and
archiver routing configuration.

The receiver emit targets the dfe-receiver contract (``src/config/mod.rs``):
``rules`` is a scalo rule set whose rows set ``_source`` (first match wins), and
the topic derives as ``routing.source_to_topic[_source]`` else
``{_source}{topic_suffix}``.

One rule evaluation drives both receiver blocks. A source's match compiles to the
row that labels the record, and on the direct transport ``destinations.sources``
keys on that same label to send it to the stage that handles it, so a source
cannot be labelled one way and routed another.

Usage:
    from dfe_engine.services.source_routing import compile_receiver_rules, compile_loader_routing

    receiver_rules = compile_receiver_rules(source_registry)
    loader_routing = compile_loader_routing(source_registry, db="prod")
"""

from dataclasses import dataclass
from typing import Any

from scalo.logger import logger

from dfe_engine.orgs.models import DEFAULT_ORG_ID
from dfe_engine.services.models.loader import LoaderRoutingConfig
from dfe_engine.services.models.receiver import (
    BUS_DESTINATION,
    LOADER_DESTINATION,
    RULE_TARGETS,
    SOURCE_FIELD,
    DestinationRef,
    DestinationsConfig,
    MatchOp,
    ReceiverRoutingConfig,
    Rule,
    RuleMatch,
    RuleSet,
    RuleSetAction,
    SourceOrgId,
)
from dfe_engine.source.flow import (
    ARCHIVER_SERVICE,
    FlowError,
    archive_gap,
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
from dfe_engine.source.reader import SourceReader

# SourceMatch.operator -> the scalo rule op the receiver row runs. The registry
# save path refuses the other operators for a receiver-routed source (see
# UnsupportedMatchOperatorError), and compile skips a stored one with a loud
# warning instead of failing the whole receiver config.
_OPERATOR_TO_OP: dict[str, MatchOp] = {
    "equals": "equals",
    "exists": "exists",
}

SOURCE_RULE_PREFIX = "source."
"""Every row a source compiles to is ``source.<name>``, apart from rows an operator writes."""


def match_op(operator: str) -> MatchOp | None:
    """The scalo rule op a source match operator compiles to, or None when it has none."""
    return _OPERATOR_TO_OP.get(operator)


@dataclass(frozen=True, slots=True)
class ReceiverMatch:
    """What the receiver tests to recognise one source's records."""

    field: str
    op: MatchOp
    value: str | None
    """The operand, or None for an op that only tests that the field is there."""


def receiver_match(source: Source) -> ReceiverMatch | None:
    """How the receiver recognises this source, or None when it needs no rule.

    The one reading of a source's match, so the row that LABELS a record cannot
    disagree with anything else about which records belong to the source.

    A fetcher-based source has no match rule of its own: its fetcher stamps
    ``_source`` with the source's landing label on every record, so the match is
    ``_source == <label>``. One landing on ``main`` needs no rule, because the
    receiver's ``default_source`` already sends an unmatched record there. An
    explicit per-source row rather than one copying ``_source``: the receiver
    takes untrusted input, and a copy would let any sender pick any topic.
    """
    if source.match is None:
        label = source.landing_label()
        if source.fetcher is None or label == DEFAULT_LANDING_LABEL:
            return None
        return ReceiverMatch(field=SOURCE_LABEL_FIELD, op="equals", value=label)

    # The main flow needs no rule: default_source already sends an unmatched
    # record there, and a match-everything rule would shadow the rules after it.
    if source.match.operator in RULELESS_OPERATORS:
        return None

    op = match_op(source.match.operator)
    if op is None:
        logger.warning(
            f"Source {source.source!r}: match operator {source.match.operator!r} has no "
            f"receiver rule op - skipping this source in the receiver routing compile"
        )
        return None

    return ReceiverMatch(
        field=source.match.field,
        op=op,
        value=source.match.value if op == "equals" else None,
    )


class UnsupportedMatchOperatorError(ValueError):
    """A receiver-routed source uses a match operator the engine compiles to no rule op."""

    def __init__(self, source: str, operator: str) -> None:
        super().__init__(
            f"Source {source!r}: match operator {operator!r} compiles to no dfe-receiver "
            f"rule. Use 'equals' or 'exists', or route this source another way."
        )
        self.source = source
        self.operator = operator


def source_rule(source: Source, match: ReceiverMatch) -> Rule:
    """The receiver row that labels *source*'s records with its landing label."""
    return Rule(
        id=f"{SOURCE_RULE_PREFIX}{source.source}",
        match=RuleMatch(field=match.field, op=match.op, value=match.value),
        set={SOURCE_FIELD: RuleSetAction(value=source.landing_label())},
    )


def compile_receiver_rules(registry: SourceReader) -> RuleSet:
    """Compile Source match rules into the receiver's ``rules`` set.

    One ``_source`` row per ACTIVE source the receiver recognises (a dormant
    source keeps its schema pre-positioned but its receiver redirect stays off),
    in source-name order, first match wins:

    - ``equals`` -> ``op: equals`` on the field, setting the landing label
    - ``exists`` -> ``op: exists`` on the field, setting the landing label
    - ``always`` -> no row at all, because that is the main flow and
      ``routing.default_source`` already labels an unmatched record with it

    No row sets ``_org_id``: a record's org comes from its sender's binding,
    else ``routing.default_org_id``.

    A stored source whose match operator has no rule op is SKIPPED with a loud
    warning rather than raised on: the registry save path rejects new ones (see
    ``UnsupportedMatchOperatorError``), and one stored doc must not brick the
    whole receiver config compile.
    """
    rules: list[Rule] = []
    for source in registry.get_all_sources(states=("active",)):
        match = receiver_match(source)
        if match is not None:
            rules.append(source_rule(source, match))
    return RuleSet(targets=list(RULE_TARGETS), rules=rules)


def compile_receiver_routing(
    registry: SourceReader,
    *,
    default_source: str = DEFAULT_LANDING_LABEL,
    topic_suffix: str = TOPIC_LAND_SUFFIX,
    source_org_id: SourceOrgId = "disallow",
) -> ReceiverRoutingConfig:
    """Compile the receiver's ``routing`` block: defaults, tenancy and topic overrides.

    ``default_org_id`` is the default org's tenant id, the same value every app
    is compiled with. ``source_org_id`` is the deployment's ``ingest.source_org_id``.

    ``source_to_topic`` is emitted only where a source's landing topic
    deviates from ``{_source}{topic_suffix}``, which is the rule the receiver
    itself applies to the label a row set - so the baseline is the landing
    LABEL, never the source name. A source landing on the shared topic is not a
    deviation: label and topic move together.
    """
    source_to_topic: dict[str, str] = {}
    for source in registry.get_all_sources(states=("active",)):
        if receiver_match(source) is None:
            continue
        expected_topic = f"{source.landing_label()}{topic_suffix}"
        if source.topic_land != expected_topic:
            source_to_topic[source.source] = source.topic_land

    return ReceiverRoutingConfig(
        default_source=default_source,
        default_org_id=DEFAULT_ORG_ID,
        topic_suffix=topic_suffix,
        source_to_topic=source_to_topic,
        source_org_id=source_org_id,
    )


def compile_receiver_destinations(registry: SourceReader, settings: Any) -> DestinationsConfig:
    """Compile where the receiver sends each labelled record, on the direct transport.

    On the bus the receiver produces to a topic and the next stage consumes it,
    so there is nothing to name: the default stays the bus and no source entry is
    emitted. On direct there is no store between stages, so every record has to
    be handed to a stage by address - its source's transform when it has one,
    else the loader. ``sources`` keys each one on the landing label its row sets.

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
    handles it, and the raw copy is kept before any transform sees it. The
    archiver's leg is declared as one that does not confirm delivery, because its
    listener answers a push once the record is queued rather than written, and the
    receiver reports its delivery guarantee from that.
    """
    default: DestinationRef = (
        LOADER_DESTINATION if settings.transport.default == "direct" else BUS_DESTINATION
    )
    sources: dict[str, DestinationRef] = {}
    named: dict[str, dict[str, Any]] = {}

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
            named[name] = {"endpoint": flow.transform.endpoint}
        else:
            # Referenced by name and given no endpoint: an entry here would win
            # over the receiver's own loader.grpc_endpoint, which is the address
            # the loader is actually deployed on.
            name = LOADER_DESTINATION

        destination: DestinationRef = name
        if flow.outputs.archive:
            gap = archive_gap(flow)
            if gap is not None:
                logger.warning(
                    f"{gap}. It is compiled as stored and archives nothing until it moves to "
                    "the bus or stops asking"
                )
            named[ARCHIVER_SERVICE] = {
                "endpoint": archiver_endpoint(settings),
                "confirms_delivery": False,
            }
            destination = [name, ARCHIVER_SERVICE]

        if receiver_match(source) is None:
            # The default flow: everything unmatched already arrives here.
            if source.match is not None and source.match.operator in RULELESS_OPERATORS:
                default = destination
            continue
        sources[source.landing_label()] = destination

    return DestinationsConfig(
        default=default,
        sources=dict(sorted(sources.items())),
        **{name: {"grpc": grpc} for name, grpc in sorted(named.items())},
    )


def compile_archiver_topics(registry: SourceReader, settings: Any) -> list[str]:
    """The landing topics the archiver reads, from the sources that asked for it.

    On the bus the archiver keeps the raw record by consuming the landing topic,
    so archiving one source and not another is this list and nothing else. An
    archiver left to a discovery pattern instead keeps every source's records
    whether it was asked to or not, which is the drift this compile removes.

    Direct sources are absent by construction: nothing holds their records, so
    the receiver fans them out to the archiver's listener instead
    (``compile_receiver_destinations``). A fetched source never passes the
    receiver, which is why the save path refuses archiving one on direct. The
    bucket and the path an object lands under stay the deployment's, so only the
    topics are derived here.
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
    registry: SourceReader,
    *,
    db: str,
    derived_base_dir: Any = None,
    schemas_base_dir: Any = None,
) -> dict[str, str]:
    """Compile each active source's derived-schema capture switches into loader modes.

    A source with no derived schema, or one whose derived schema keeps both
    switches on, contributes nothing: the loader's global ``capture_mode``
    already populates both columns.

    The key is the table the loader writes to, qualified the way the loader
    matches it (``<db>.<table>``), so the value the engine emits here is the one
    dfe-loader looks up.

    Args:
        registry: Where the Source definitions are read from.
        db: Default database the loader writes to.
        derived_base_dir: Root a ``derived/...`` reference resolves under
            (``DerivedSchemaRegistry.reference_root``).
        schemas_base_dir: The schemas tree, where a derived schema the release
            ships resolves when the deployment's own store has no copy - the
            same fallback the table build takes.
    """
    from dfe_engine.schema.derived import CAPTURE_MODE_FULL, capture_mode
    from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2
    from dfe_engine.schema.schema_loader import SchemaLoader, SchemaLoadError

    builder = SchemaBuilderV2(schemas_base_dir=schemas_base_dir, derived_base_dir=derived_base_dir)
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
    registry: SourceReader,
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

    ``default_org_id`` is the default org's tenant id, the same value the
    receiver and fetcher are compiled with, so a record no first hop stamped
    lands in the org every other app would have put it in.

    Args:
        registry: Where the Source definitions are read from.
        db: Default database for the loader.
    """
    source_to_table: dict[str, str] = {}
    for source in registry.get_all_sources(states=("active",)):
        source_to_table[source.source] = source.table_name

    return LoaderRoutingConfig(
        default_db=db,
        default_org_id=DEFAULT_ORG_ID,
        source_to_table=source_to_table,
    )
