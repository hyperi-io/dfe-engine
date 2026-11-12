#  Project:      dfe-engine
#  File:         services/routing_models.py
#  Purpose:      The receiver and loader routing blocks the source compile emits
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The routing blocks ``source_routing`` compiles, in the shape each app reads them.

Each model is one block of dfe-receiver's or dfe-loader's own config, and the
compile dumps only the fields it set, so a default here never reaches an app.
"""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dfe_engine.orgs.models import DEFAULT_ORG_ID
from dfe_engine.source.models import DEFAULT_LANDING_LABEL


class DlqConfig(BaseModel):
    """Dead Letter Queue configuration for the receiver's routing block."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # The per-app topic dfe-schemas declares and the engine creates at bootstrap.
    topic: str = "dfe_receiver_dlq"


SOURCE_FIELD = "_source"
ORG_FIELD = "_org_id"

RULE_TARGETS = (SOURCE_FIELD, ORG_FIELD)
"""The fields the receiver's rules set, in the order it evaluates them."""

SourceOrgId = Literal["disallow", "allow"]
"""Whether the receiver keeps an ``_org_id`` the sender wrote, held to the sender's binding."""

MatchOp = Literal[
    "equals",
    "starts_with",
    "ends_with",
    "contains",
    "prefix_and_suffix",
    "length",
    "exists",
    "regex",
]
"""scalo-rs ``rules::MatchOp``: the native tests a rule row can run."""


class RuleMatch(BaseModel):
    """scalo-rs ``rules::MatchConfig``: a native test on one field, or ``all`` of several."""

    model_config = ConfigDict(extra="forbid")

    field: str | None = Field(default=None, description="Dotted path of the field tested")
    op: MatchOp | None = None
    value: str | None = Field(default=None, description="Operand of the text tests")
    prefix: str | None = None
    suffix: str | None = None
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)
    negate: bool = Field(default=False, description="Invert the result")
    all: list[RuleMatch] = Field(default_factory=list, description="Every one of these")


class RuleSetAction(BaseModel):
    """scalo-rs ``rules::SetAction``: how a matching row finds the value it sets."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)

    value: str | None = Field(default=None, description="This literal text")
    from_field: str | None = Field(default=None, alias="from", description="This field's text")
    lookup: str | None = Field(default=None, description="The lookup table `from` maps through")


class Rule(BaseModel):
    """scalo-rs ``rules::RuleConfig``: one row, setting one target field when it matches."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="Stable id: the rule metric label")
    match: RuleMatch | None = Field(default=None, description="A native test")
    when: str | None = Field(default=None, description="A CEL Fast condition, in place of match")
    set: dict[str, RuleSetAction] = Field(description="Exactly one target field and its value")

    @field_validator("set")
    @classmethod
    def _one_target(cls, v: dict[str, RuleSetAction]) -> dict[str, RuleSetAction]:
        """Refuse a row that sets no field, several, or one the receiver does not set."""
        if len(v) != 1:
            raise ValueError("a rule sets exactly one field")
        target = next(iter(v))
        if target not in RULE_TARGETS:
            raise ValueError(f"a receiver rule sets one of {', '.join(RULE_TARGETS)}, not {target}")
        return v


class RuleSet(BaseModel):
    """scalo-rs ``rules::RuleSetConfig``: per target field, the first matching row sets it.

    dfe-receiver reads it as ``rules`` and sets ``_source`` then ``_org_id``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = "rules"
    targets: list[str] = Field(default_factory=list, description="Empty is _source, _org_id")
    lookups: dict[str, dict[str, str]] = Field(default_factory=dict)
    rules: list[Rule] = Field(default_factory=list, description="The rows, in declared order")

    @field_validator("targets")
    @classmethod
    def _receiver_targets(cls, v: list[str]) -> list[str]:
        """Refuse targets the receiver would refuse: anything but _source then _org_id."""
        if v and tuple(v) != RULE_TARGETS:
            raise ValueError(f"the receiver sets {', '.join(RULE_TARGETS)}, in that order")
        return v


class ReceiverRoutingConfig(BaseModel):
    """Message routing configuration - the dfe-receiver ``RoutingConfig`` contract.

    The receiver refuses a key it does not know here. Topic is
    ``source_to_topic[_source]`` else ``{_source}{topic_suffix}``.
    """

    model_config = ConfigDict(extra="forbid")

    default_source: str = Field(
        default=DEFAULT_LANDING_LABEL, description="_source when no rule matches"
    )
    default_org_id: str = Field(
        default=DEFAULT_ORG_ID,
        min_length=1,
        description="_org_id an unbound sender's records take, and the only org they may land in",
    )
    topic_suffix: str = Field(
        default="_land", description="Suffix appended to _source to form the topic"
    )
    source_to_topic: dict[str, str] = Field(
        default_factory=dict,
        description="Optional per-source topic overrides ({source: topic})",
    )
    source_org_id: SourceOrgId = Field(
        default="disallow",
        description="disallow strips a sender's _org_id; allow keeps it, held to its binding",
    )
    dlq: DlqConfig = Field(default_factory=DlqConfig)


# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------


BUS_DESTINATION = "kafka"
LOADER_DESTINATION = "loader"

BUILT_IN_DESTINATIONS = frozenset({BUS_DESTINATION, LOADER_DESTINATION})
"""The two destinations the receiver resolves without being given an address."""

DestinationRef = str | list[str]
"""One destination name, or several to fan a matched record out to.

The receiver's own ``DestinationRef`` (``src/config/mod.rs``), an untagged enum of
a string or a list of them. A fan-out is delivered once EVERY destination has
accepted the record, which is what lets one rule reach the loader and the
archiver at the same time.
"""


def destination_names(ref: DestinationRef) -> list[str]:
    """The names in a reference, one or many."""
    return [ref] if isinstance(ref, str) else list(ref)


class DestinationsConfig(BaseModel):
    """Destination selection, plus the endpoint of every destination it names.

    ``kafka`` and ``loader`` are the receiver's built-in destinations. Any other
    name is a NAMED endpoint sitting beside these keys as
    ``<name>: {grpc: {endpoint: uri}}``, which is why extras are allowed here: a
    source sends its records to a transform instance by name. The names the
    engine compiles are ``loader``, ``dfe-archiver`` and ``dfe-transform-*``
    instances, so none of them can collide with ``default`` or ``sources``.

    ``sources`` keys on the ``_source`` the rules set, so the one rule evaluation
    that labels a record also picks where it goes. A source may name several
    destinations at once, which is how an archived source reaches the
    archiver beside the stage that loads it. The archiver's entry is
    ``dfe-archiver: {grpc: {endpoint: uri, confirms_delivery: false}}``: its Push
    listener answers once a record is queued, not written, so the receiver must not
    count that leg towards the at-least-once guarantee it reports.

    The nesting is the receiver's own ``DestinationSpec`` (``src/config/mod.rs``):
    one transport block per destination, ``grpc: {endpoint}`` or ``kafka: {topic}``.
    A bare ``grpc: <uri>`` is what the receiver read before v1.15.30 and it now
    refuses the whole config file, so the pod cannot start.
    """

    model_config = ConfigDict(extra="allow")

    default: DestinationRef = Field(
        default=BUS_DESTINATION,
        description="kafka, loader, a named destination, or a list of them to fan out",
    )
    sources: dict[str, DestinationRef] = Field(
        default_factory=dict,
        description="Destinations by the _source a rule set; any other _source takes default",
    )

    @model_validator(mode="after")
    def validate_destinations(self) -> Self:
        """Refuse a destination the receiver could not resolve or deliver to."""
        named = self.__pydantic_extra__ or {}
        for name, entry in named.items():
            grpc = entry.get("grpc") if isinstance(entry, dict) else None
            if not isinstance(grpc, dict) or not grpc.get("endpoint"):
                msg = (
                    f"Named destination {name!r} must be a mapping carrying "
                    f"grpc.endpoint; the receiver refuses a bare grpc string"
                )
                raise ValueError(msg)
        # An empty fan-out names nowhere to deliver to, so the receiver answers every
        # record it routes there delivered and sends it nowhere.
        refs = {"default": self.default} | {f"sources.{s}": r for s, r in self.sources.items()}
        for key, ref in refs.items():
            if not destination_names(ref):
                msg = f"destinations.{key} is an empty list: name at least one destination"
                raise ValueError(msg)
        # A destination the receiver cannot resolve silently drops every record
        # sent to it, so nothing may name one that is neither built in nor
        # declared above.
        known = BUILT_IN_DESTINATIONS | set(named)
        unknown = {n for ref in self.sources.values() for n in destination_names(ref)} | set(
            destination_names(self.default)
        )
        if unknown - known:
            msg = (
                f"Destinations not declared: {', '.join(sorted(unknown - known))}. "
                f"Known: {', '.join(sorted(known))}"
            )
            raise ValueError(msg)
        return self


class LoaderDlqConfig(BaseModel):
    """DLQ configuration for the loader."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # Non-empty routes every dead letter here. The per-app topic dfe-schemas
    # declares and the engine creates at bootstrap.
    topic: str = "dfe_loader_dlq"
    # Read only when topic is empty, as <destination><suffix>.
    topic_suffix: str = ".dlq"


class LoaderExternalTopic(BaseModel):
    """One topic a customer's own pipeline writes, and the labels its records may keep.

    The loader's ``ExternalTopic`` entry: on this topic a record whose ``_org_id`` is
    neither ``org_id`` nor listed in ``allowed_org_ids`` is given ``org_id``, and the
    same for ``_source``. Each replacement is counted and the record is never refused.
    """

    model_config = ConfigDict(extra="forbid")

    topic: str
    org_id: str
    allowed_org_ids: list[str] = Field(default_factory=list)
    source: str
    allowed_sources: list[str] = Field(default_factory=list)


class LoaderRoutingConfig(BaseModel):
    """Database/table routing configuration for the loader.

    Field-for-field the loader's ``RoutingConfig``
    (dfe-loader/src/config/pipeline.rs), DEFAULTS INCLUDED. That struct is the
    contract, and serde drops an unknown key without a word, so a key invented
    here does not fail the loader -- it silently never takes effect and the
    loader falls back to its own default, sending every message somewhere the
    author never asked for. The Rust struct is the SSoT. Anything added here
    must exist there first.

    The table comes from the first ``table_fields`` hit (mapped through
    ``source_to_table`` when it has an entry), else ``default_table``. The tenant
    is the ``org_id_field`` value, else ``default_org_id``, and ``org_routes``
    gives a tenant its own database, every other one taking ``default_db``.
    """

    model_config = ConfigDict(extra="forbid")

    table_fields: list[str] = Field(default_factory=lambda: ["_source"])
    default_db: str = "dfe"
    # The key names the fallback mechanism. Its value is the landing table's name.
    default_table: str = DEFAULT_LANDING_LABEL
    org_id_field: str | None = "_org_id"
    default_org_id: str = Field(
        default=DEFAULT_ORG_ID,
        min_length=1,
        description="Tenant for a record whose org_id_field is absent, null or empty",
    )
    org_routes: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Per-org database routing {org_id, database?}; unlisted orgs use default_db",
    )
    source_to_table: dict[str, str] = Field(default_factory=dict)
    topic_suffixes: list[str] = Field(default_factory=lambda: ["_land", "_load"])
    dlq: LoaderDlqConfig = Field(default_factory=LoaderDlqConfig)
    external_topics: list[LoaderExternalTopic] = Field(
        default_factory=list,
        description="The topics a customer's own pipeline writes, each held to its source's org and name",
    )
