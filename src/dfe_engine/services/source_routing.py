"""Source routing config generator.

Compiles Source definitions from SourceRegistry into receiver, loader and
archiver routing configuration.

The receiver emit targets the dfe-receiver contract (``src/config/mod.rs``):
``rules`` is a scalo rule set whose rows set ``_source`` then ``_org_id`` (first
match per field wins), and the topic derives as
``routing.source_to_topic[_source]`` else ``{_source}{topic_suffix}``.

The rule set is built from blocks. Each active source's match or ``when``
compiles to one block of rows, and each bound compatibility profile to one block
per field it sets. The deployment's rule order places the blocks, and the
receiver's own default runs last for each field.

One rule evaluation drives both receiver blocks. A source's match compiles to the
rows that label the record, and on the direct transport ``destinations.sources``
keys on that same label to send it to the stage that handles it, so a source
cannot be labelled one way and routed another.

Usage:
    from dfe_engine.services.source_routing import compile_receiver_rules, compile_loader_routing

    receiver_rules = compile_receiver_rules(source_registry)
    loader_routing = compile_loader_routing(source_registry, db="prod")
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError
from scalo.expression import ProfileConfig
from scalo.logger import logger
from scalo.rules import CompileOptions, RuleDiagnostic
from scalo.rules import RuleSet as ScaloRuleSet

from dfe_engine.ingest.models import ALIASES_TABLE, IngestRules
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
    CompatBinding,
    Source,
    SourceMatch,
    SourceVersion,
)
from dfe_engine.source.reader import SourceReader

RECEIVER_RULES_PROFILE = ProfileConfig()
"""The CEL Fast profile dfe-receiver compiles ``when`` under.

The engine renders the receiver no ``expression`` section, so scalo's default
applies: regex, iteration and time functions are refused.
"""

# SourceMatch.operator -> the scalo test a receiver row runs, and whether it is inverted.
_OPERATOR_TESTS: dict[str, tuple[MatchOp, bool]] = {
    "equals": ("equals", False),
    "not_equals": ("equals", True),
    "exists": ("exists", False),
    "includes": ("contains", False),
    "starts_with": ("starts_with", False),
    "ends_with": ("ends_with", False),
}

SOURCE_RULE_PREFIX = "source."
"""A source's block is ``source.<name>``, and so is its row, or ``source.<name>.<n>`` for several."""

COMPAT_RULE_PREFIX = "compat."
"""A profile's block is ``compat.<profile>``, its rows ``compat.<profile>.<row>[.<source>]``."""

DEFAULT_BLOCK = "default"
"""The receiver's own default for a field, pinned after every block."""

MAX_SOURCE_ROWS = 32
"""The most rows one source's match may compile to: each ``any`` branch is a row of its own."""

MAX_RULE_ID_LENGTH = 128
"""scalo.rules refuses a rule id longer than this."""

BlockKind = Literal["source", "profile", "default"]


class MatchTooWideError(ValueError):
    """A source's match compiles to more rows than one source may hold."""


@dataclass(frozen=True, slots=True)
class RuleBlock:
    """One unit of the rule order: a source's rows, a profile's rows, or the receiver's default.

    Attributes:
        id: ``source.<name>``, ``compat.<profile>`` or ``default``.
        target: The field the rows set.
        kind: What the block is.
        name: The source's name or the profile's id, empty for the default.
        rules: The rows, in the order they fire.
        version: The profile version compiled, for a profile block.
    """

    id: str
    target: str
    kind: BlockKind
    name: str = ""
    rules: tuple[Rule, ...] = ()
    version: str | None = None

    @property
    def pinned(self) -> bool:
        """Whether the block keeps its place whatever the order says."""
        return self.kind == "default"


@dataclass(frozen=True, slots=True)
class CompiledRules:
    """The receiver's rule set, the blocks it came from, and what was found wrong with them.

    Attributes:
        rule_set: The rule set as the receiver reads it.
        blocks: Per target, every block in the order it runs, the default last. A
            dropped block keeps its place here and has no rows in ``rule_set``.
        diagnostics: The rule set's warnings, and the errors that dropped a block.
        dropped: The blocks left out because their rows did not compile.
        warnings: Per block id, every scalo.rules finding about its rows, each led by
            the row it names.
    """

    rule_set: RuleSet
    blocks: dict[str, list[RuleBlock]]
    diagnostics: tuple[RuleDiagnostic, ...] = ()
    dropped: tuple[str, ...] = ()
    warnings: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


def _test(match: SourceMatch, inverted: bool) -> RuleMatch:
    """One receiver test for a test in a source's match."""
    if match.operator not in _OPERATOR_TESTS:
        raise ValueError(f"match operator {match.operator!r} compiles to no receiver test")
    op, flipped = _OPERATOR_TESTS[match.operator]
    return RuleMatch(
        field=match.field,
        op=op,
        value=None if op == "exists" else match.value,
        negate=flipped != inverted,
    )


def _conjoin(parts: list[RuleMatch]) -> RuleMatch:
    """One row's test from the tests that must all hold, flattening a plain ``all``."""
    flat: list[RuleMatch] = []
    for part in parts:
        if part.all and not part.negate:
            flat.extend(part.all)
        else:
            flat.append(part)
    return flat[0] if len(flat) == 1 else RuleMatch(all=flat)


def _capped[T](rows: list[T]) -> list[T]:
    if len(rows) > MAX_SOURCE_ROWS:
        raise MatchTooWideError(
            f"the match compiles to more than {MAX_SOURCE_ROWS} receiver rows: an any inside "
            "an all repeats every other part once per branch, so move the any outward"
        )
    return rows


def _every(groups: list[list[RuleMatch]]) -> list[RuleMatch]:
    """Rows holding where one row of each group holds: every combination, in order."""
    combos: list[list[RuleMatch]] = [[]]
    for rows in groups:
        grown: list[list[RuleMatch]] = []
        for combo in combos:
            for row in rows:
                grown.append([*combo, row])
        combos = _capped(grown)
    return [_conjoin(combo) for combo in combos]


def _rows(match: SourceMatch, inverted: bool) -> list[RuleMatch]:
    """The rows *match* compiles to, any one of which holding is a match.

    A negated group is pushed through its parts by de Morgan's laws, because the
    receiver has no ``any``: a record matches when one of the source's rows does.
    """
    flip = match.negate != inverted
    if match.is_test:
        return [_test(match, flip)]
    if match.all and flip:
        # NOT (a and b) stays one negated row while no part branches into several.
        plain = [_rows(part, False) for part in match.parts]
        if all(len(rows) == 1 for rows in plain):
            return [RuleMatch(all=[rows[0] for rows in plain], negate=True)]
    parts = [_rows(part, flip) for part in match.parts]
    if bool(match.all) != flip:
        return _every(parts)
    joined: list[RuleMatch] = []
    for rows in parts:
        joined.extend(rows)
    return _capped(joined)


def match_rows(match: SourceMatch) -> list[RuleMatch]:
    """The receiver tests a source's match compiles to, one row each, any one matching.

    ``all`` and ``negate`` compile to one row. Each branch of an ``any`` is a row
    of its own, in declared order, so the rows a match compiles to never churn.

    Raises:
        MatchTooWideError: The match compiles to more than ``MAX_SOURCE_ROWS`` rows.
    """
    return _capped(_rows(match, False))


def version_rules(name: str, label: str, version: SourceVersion) -> list[Rule]:
    """The receiver rows one source version compiles to, empty when it needs none.

    A fetcher-based source has no match of its own: its fetcher stamps ``_source``
    with the landing label on every record, so its row is ``_source == <label>``.
    One landing on ``main`` needs no row, because the receiver's ``default_source``
    already sends an unmatched record there, and neither does ``always``, which is
    that main flow. A copy of ``_source`` is never a row: the receiver takes
    untrusted input, and a copy would let any sender pick any topic.

    Args:
        name: The source's name, which names its rows.
        label: The ``_source`` its rows set.
        version: The version compiled.

    Raises:
        MatchTooWideError: The match compiles to more rows than a source may hold.
    """
    action = {SOURCE_FIELD: RuleSetAction(value=label)}
    prefix = f"{SOURCE_RULE_PREFIX}{name}"
    if version.when is not None:
        return [Rule(id=prefix, when=version.when, set=action)]
    match = version.match
    if match is None:
        if version.fetcher is None or label == DEFAULT_LANDING_LABEL:
            return []
        rows = [RuleMatch(field=SOURCE_LABEL_FIELD, op="equals", value=label)]
    elif match.is_test and match.operator in RULELESS_OPERATORS:
        return []
    else:
        rows = match_rows(match)
    if len(rows) == 1:
        return [Rule(id=prefix, match=rows[0], set=action)]
    return [
        Rule(id=f"{prefix}.{index}", match=row, set=action)
        for index, row in enumerate(rows, start=1)
    ]


def source_rules(source: Source) -> list[Rule]:
    """The receiver rows *source* compiles to, from the version the receiver runs.

    Raises:
        MatchTooWideError: The match compiles to more rows than a source may hold.
    """
    return version_rules(source.source, source.landing_label(), source.version())


def has_receiver_rule(source: Source) -> bool:
    """Whether the receiver labels *source*'s records with a row of its own."""
    try:
        return bool(source_rules(source))
    except MatchTooWideError:
        return False


def check_rule_set(rule_set: RuleSet) -> list[RuleDiagnostic]:
    """Every finding scalo.rules makes of *rule_set*, under the receiver's CEL Fast profile."""
    return ScaloRuleSet.check(
        rule_set.model_dump(mode="json", exclude_defaults=True),
        CompileOptions(profile=RECEIVER_RULES_PROFILE),
    )


def effective_bindings(source: Source, ingest: IngestRules) -> list[CompatBinding]:
    """The profiles *source* binds: its own list, else the deployment's."""
    own = source.compat_profiles
    return list(own) if own is not None else list(ingest.defaults)


@dataclass(frozen=True, slots=True)
class _Bound:
    source: Source
    binding: CompatBinding


def _profile_rule(
    rule_id: str, row: Mapping[str, Any], tables: Mapping[str, str], guard: str | None
) -> Rule:
    """One receiver row for a profile row, its lookups renamed and held to *guard*'s records."""
    match = row.get("match")
    when = row.get("when")
    target, action = next(iter(row["set"].items()))
    action = dict(action)
    if "lookup" in action:
        action["lookup"] = tables.get(action["lookup"], action["lookup"])
    if guard is not None:
        test = {"field": SOURCE_FIELD, "op": "equals", "value": guard}
        if when is not None:
            when = f'{SOURCE_FIELD} == "{guard}" && ({when})'
        elif match is not None:
            match = {"all": [test, match]}
        else:
            match = test
    return Rule.model_validate(
        {"id": rule_id, "match": match, "when": when, "set": {target: action}}
    )


def _aliases(profile: str, bound: list[_Bound]) -> dict[str, str]:
    """The alias table a profile reads: each bound source's aliases, mapped to its label."""
    table: dict[str, str] = {}
    for item in bound:
        for alias in item.source.aliases.get(profile, []):
            table[alias] = item.source.landing_label()
    return dict(sorted(table.items()))


def _one_profile(
    profile_id: str, bound: list[_Bound], ingest: IngestRules, lookups: dict[str, dict[str, str]]
) -> list[RuleBlock]:
    """A bound profile's blocks, one per field it sets. Empty, and logged, where it cannot compile."""
    profile = ingest.profiles.get(profile_id)
    if profile is None:
        logger.warning(f"compatibility profile {profile_id!r} is bound but not stored: skipped")
        return []
    versions = sorted({profile.resolve(item.binding.version) for item in bound})
    if len(versions) != 1 or versions[0] not in profile.versions:
        logger.warning(
            f"compatibility profile {profile_id!r} is bound at {', '.join(versions)}, and one "
            "version per deployment compiles: skipped"
        )
        return []
    version = versions[0]
    entry = profile.versions[version]
    if entry.unsupported_targets():
        logger.warning(
            f"compatibility profile {profile_id!r} sets {', '.join(entry.unsupported_targets())}, "
            "which the receiver's rules do not: skipped"
        )
        return []
    tables = {name: f"{COMPAT_RULE_PREFIX}{profile_id}.{name}" for name in entry.lookups}
    for name, shipped in entry.lookups.items():
        filled = _aliases(profile_id, bound) if name == ALIASES_TABLE else dict(shipped)
        lookups[tables[name]] = filled
    offered = set(entry.opt_in)
    rows: dict[str, list[Rule]] = {target: [] for target in RULE_TARGETS}
    prefix = f"{COMPAT_RULE_PREFIX}{profile_id}"
    try:
        for row in entry.rules:
            row_id = row["id"]
            target = entry.row_target(row)
            if target == SOURCE_FIELD:
                wanted = row_id not in offered or any(
                    row_id in item.binding.opt_in for item in bound
                )
                if wanted:
                    rows[target].append(_profile_rule(f"{prefix}.{row_id}", row, tables, None))
                continue
            for item in bound:
                if row_id in offered and row_id not in item.binding.opt_in:
                    continue
                rule_id = f"{prefix}.{row_id}.{item.source.source}"
                guard = item.source.landing_label()
                rows[target].append(_profile_rule(rule_id, row, tables, guard))
    except ValidationError as exc:
        logger.warning(
            f"compatibility profile {profile_id!r} has a row the receiver refuses: {exc}"
        )
        for table in tables.values():
            lookups.pop(table, None)
        return []
    return [
        RuleBlock(
            id=prefix,
            target=target,
            kind="profile",
            name=profile_id,
            rules=tuple(found),
            version=version,
        )
        for target, found in rows.items()
        if found
    ]


def _profile_blocks(
    sources: list[Source], ingest: IngestRules, lookups: dict[str, dict[str, str]]
) -> list[RuleBlock]:
    """Every bound profile's blocks, by profile id."""
    by_profile: dict[str, list[_Bound]] = {}
    for source in sources:
        for binding in effective_bindings(source, ingest):
            by_profile.setdefault(binding.profile, []).append(_Bound(source, binding))
    blocks: list[RuleBlock] = []
    for profile_id in sorted(by_profile):
        blocks.extend(_one_profile(profile_id, by_profile[profile_id], ingest, lookups))
    return blocks


def _source_blocks(sources: list[Source]) -> list[RuleBlock]:
    """Every active source's block, by name. A source too wide to compile is skipped loudly."""
    blocks: list[RuleBlock] = []
    for source in sources:
        try:
            rules = source_rules(source)
        except MatchTooWideError as exc:
            logger.warning(f"Source {source.source!r} skipped in the receiver rules: {exc}")
            continue
        if rules:
            blocks.append(
                RuleBlock(
                    id=f"{SOURCE_RULE_PREFIX}{source.source}",
                    target=SOURCE_FIELD,
                    kind="source",
                    name=source.source,
                    rules=tuple(rules),
                )
            )
    return blocks


def _ordered(blocks: list[RuleBlock], named: Iterable[str]) -> list[RuleBlock]:
    """*blocks* in the order *named* gives them, the ones it leaves out after, as they came."""
    by_id = {block.id: block for block in blocks}
    first = [by_id[block_id] for block_id in dict.fromkeys(named) if block_id in by_id]
    placed = {block.id for block in first}
    return first + [block for block in blocks if block.id not in placed]


def _rule_set(blocks: dict[str, list[RuleBlock]], lookups: dict[str, dict[str, str]]) -> RuleSet:
    rules: list[Rule] = []
    for target in RULE_TARGETS:
        for block in blocks[target]:
            rules.extend(block.rules)
    used = {rule.set[next(iter(rule.set))].lookup for rule in rules}
    tables = {name: lookups[name] for name in sorted(lookups) if name in used}
    return RuleSet(targets=list(RULE_TARGETS), lookups=tables, rules=rules)


def _owner(diagnostic: RuleDiagnostic, blocks: dict[str, list[RuleBlock]]) -> str | None:
    """The block a finding is about: the one holding its rule, or using its lookup table."""
    for target in RULE_TARGETS:
        for block in blocks[target]:
            if diagnostic.rule is not None and any(r.id == diagnostic.rule for r in block.rules):
                return block.id
            prefix = f"lookups.{block.id}."
            if diagnostic.rule is None and diagnostic.location.startswith(prefix):
                return block.id
    return None


def _note(diagnostic: RuleDiagnostic) -> str:
    """A finding as a block's warning: the row it is about, then what is wrong."""
    return f"{diagnostic.rule or diagnostic.location}: {diagnostic.message}"


def compile_ingest_rules(sources: Iterable[Source], ingest: IngestRules) -> CompiledRules:
    """Compile active sources and their bound profiles into the receiver's rule set.

    Blocks run in the deployment's order. One the order does not name runs after
    every one it does, sources by name before profiles by id. A block whose rows
    scalo.rules refuses is left out loudly, because the receiver refuses a whole
    rule set over one bad row, and one stored document must not brick it. What
    scalo.rules warns of, a row an earlier one always shadows among it, is kept
    against the block it is about.

    Raises:
        RuntimeError: scalo.rules refuses the set for a reason no block owns.
    """
    ordered_sources = sorted(sources, key=lambda source: source.source)
    lookups: dict[str, dict[str, str]] = {}
    found = _source_blocks(ordered_sources) + _profile_blocks(ordered_sources, ingest, lookups)
    placed = {
        target: _ordered(
            [block for block in found if block.target == target], ingest.order.ids(target)
        )
        for target in RULE_TARGETS
    }
    blocks = placed
    dropped: list[str] = []
    dropping: list[RuleDiagnostic] = []
    while True:
        diagnostics = check_rule_set(_rule_set(blocks, lookups))
        errors = [diagnostic for diagnostic in diagnostics if diagnostic.is_error]
        if not errors:
            break
        owners = {_owner(error, blocks) for error in errors}
        if None in owners:
            raise RuntimeError(
                "the receiver rule set does not compile: "
                + ". ".join(str(error) for error in errors if _owner(error, blocks) is None)
            )
        for error in errors:
            logger.warning(f"receiver rule block {_owner(error, blocks)!r} left out: {error}")
        dropping.extend(errors)
        dropped.extend(sorted(str(owner) for owner in owners))
        blocks = {
            target: [block for block in blocks[target] if block.id not in owners]
            for target in RULE_TARGETS
        }
    warnings: dict[str, list[str]] = {}
    for diagnostic in (*dropping, *diagnostics):
        owner = _owner(diagnostic, placed)
        if owner is not None:
            warnings.setdefault(owner, []).append(_note(diagnostic))
    listed = {
        target: [*placed[target], RuleBlock(id=DEFAULT_BLOCK, target=target, kind="default")]
        for target in RULE_TARGETS
    }
    return CompiledRules(
        rule_set=_rule_set(blocks, lookups),
        blocks=listed,
        diagnostics=(*dropping, *diagnostics),
        dropped=tuple(dropped),
        warnings={owner: tuple(messages) for owner, messages in sorted(warnings.items())},
    )


def compile_rules(registry: SourceReader) -> CompiledRules:
    """Compile the registry's active sources and ingest rules into the receiver's rule set."""
    return compile_ingest_rules(
        registry.get_all_sources(states=("active",)), registry.ingest_rules()
    )


def compile_receiver_rules(registry: SourceReader) -> RuleSet:
    """Compile Source match rules into the receiver's ``rules`` set.

    Each ACTIVE source the receiver recognises (a dormant source keeps its schema
    pre-positioned but its receiver redirect stays off) compiles to rows setting
    ``_source`` to its landing label, first match wins:

    - a test compiles to one native test on the field, ``not_equals`` as a negated
      ``equals`` and ``includes`` as ``contains``
    - ``all`` and ``negate`` compile to one row, and each ``any`` branch to a row
    - ``when`` compiles to one row carrying the CEL Fast condition
    - ``always`` is no row at all, because that is the main flow and
      ``routing.default_source`` already labels an unmatched record with it

    A bound compatibility profile adds its rows: ones setting ``_source`` for the
    whole deployment, ones setting ``_org_id`` once per bound source, each held to
    that source's records. With no profile bound no row sets ``_org_id``, and a
    record's org comes from its sender's binding, else ``routing.default_org_id``.
    """
    return compile_rules(registry).rule_set


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
        if not has_receiver_rule(source):
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

        if not has_receiver_rule(source):
            # The default flow: everything unmatched already arrives here.
            match = source.match
            if match is not None and match.is_test and match.operator in RULELESS_OPERATORS:
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
