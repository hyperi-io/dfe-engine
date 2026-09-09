"""Source model — Pydantic models for the Source top-level data entity.

A Source represents a data stream entering the DFE platform (e.g. filebeat,
syslog, crowdstrike_edr). It contains identity, schema, naming-standard views,
and optional fetcher/transform configuration.

See docs/data-plane/source.md for the full specification.

Usage:
    from dfe_engine.source.models import Source, SchemaColumn

    source = Source.model_validate(yaml_data)
    source_dict = source.model_dump(mode="json")
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from dfe_engine.api.pagination import PaginatedResponseWithObjects, PathTree
from dfe_engine.source.engine_registry import EngineRegistry, InvalidEngineError
from dfe_engine.transport import SourceTransport

# _source naming: a Kubernetes DNS-1123 label that starts with a letter. A
# source-bound app's instance name IS the source name, so this charset must stay
# a subset of ``appmgmt.instances._INSTANCE_RE`` -- hyphens, never underscores.
_SOURCE_PATTERN = re.compile(r"^[a-z]([a-z0-9-]*[a-z0-9])?$")

# The instance label caps at 40 characters, and the source name has to fit one.
_SOURCE_MAX_LENGTH = 40


def validate_source_name(name: str) -> str:
    """Return *name* if it is a legal ``_source`` label, else raise.

    The single definition of the rule. Anything else holding a source label --
    a field map's ``source``, a rule's ``source`` -- validates through here, so
    the two ends can never drift into accepting different names for one thing.
    """
    if len(name) > _SOURCE_MAX_LENGTH:
        raise ValueError(
            f"Source name {name!r} exceeds max length of {_SOURCE_MAX_LENGTH}: a "
            "source-bound app deploys an instance named for its source, and a "
            "Kubernetes label cannot be longer than that"
        )
    if not _SOURCE_PATTERN.match(name):
        hint = (
            " (use '-' instead of '_')"
            if "_" in name
            else " (lowercase alphanumeric and '-', starting with a letter, ending alphanumeric)"
        )
        raise ValueError(
            f"Source name {name!r} must be a Kubernetes DNS-1123 label starting with a "
            f"letter{hint}: a source-bound app such as a transform or a fetcher is "
            "deployed as one instance per source, named for the source, so the name "
            "becomes an Argo Application and a set of Kubernetes object names"
        )
    return name


SourceMatchOperator = Literal[
    "equals",
    "not_equals",
    "exists",
    "includes",
    "starts_with",
    "ends_with",
    "always",
]

# Operators that compare nothing, so ``match.value`` carries no operand.
OPERATORS_WITHOUT_OPERAND = frozenset({"exists", "always"})

RULELESS_OPERATORS = frozenset({"always"})
"""Operators the receiver honours WITHOUT a rule.

``always`` is the default flow: an unmatched record already goes to the
receiver's ``default_source``, so a rule matching everything would shadow every
rule after it. Reserved for the ``default`` source.
"""

# Tri-state source lifecycle:
# - active   - schema materialised + receiver redirect + transform all ON.
# - dormant  - schema RETAINED (pre-positioned), redirect + transform OFF.
# - disabled - all off, table reclaimed (== the old enabled=False).
SourceState = Literal["active", "dormant", "disabled"]


def state_from_enabled(enabled: bool) -> SourceState:
    """The tri-state a legacy boolean maps to (True -> active, False -> disabled)."""
    return "active" if enabled else "disabled"


def materialisation_action(state: SourceState) -> Literal["create", "leave", "reclaim"]:
    """Lazy materialisation rule (locked): CREATE on active / LEAVE on dormant /
    guarded RECLAIM on disabled.

    Creates are idempotent (no has-been-materialised bit), dormant is a no-op
    (never drop a dormant source's table - the pre-2.2 bug), and a drop happens
    only on disabled behind the caller's guard. Gitops-declarative: the action
    derives from the declared state alone.
    """
    actions: dict[str, Literal["create", "leave", "reclaim"]] = {
        "active": "create",
        "dormant": "leave",
        "disabled": "reclaim",
    }
    return actions[state]


# Single permitted-engine registry, shared by every SourceSchema validation so the
# allow-list cannot drift from the DDL path. Loaded once (the YAML is packaged).
_ENGINE_REGISTRY = EngineRegistry.default()


# ---------------------------------------------------------------------------
# Schema Column (used in meta_schema, derived_schema, additional_fields)
# ---------------------------------------------------------------------------


class SchemaColumn(BaseModel):
    """A single column in a schema definition.

    See docs/data-plane/schema.md for the column model:
    type + attribute + use_case + expr + comment
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., description="Column name")
    type: str = Field(..., description="Primitive type (string, integer, etc.)")
    attribute: list[str] = Field(
        default_factory=list,
        description="Storage attributes (lowcardinality, nullable, etc.)",
    )
    use_case: str | None = Field(
        default=None,
        description="Query use case (dimension, fulltext, range, etc.)",
    )
    default: str | None = Field(
        default=None,
        description="DEFAULT expression",
    )
    order: int | None = Field(
        default=None,
        description="Position in ORDER BY / PRIMARY KEY",
    )
    expr: str | None = Field(
        default=None,
        description="DFE directive (@source, @generated, @captured, @computed, @config)",
    )
    comment: str | None = Field(
        default=None,
        description="Human-readable column description",
    )
    field_type: str | None = Field(
        default=None,
        validation_alias=AliasChoices("_field_type", "field_type"),
        serialization_alias="_field_type",
        description="Column classification (e.g. base); stored as _field_type in YAML",
    )
    ch_override: str | None = Field(
        default=None,
        description="Exact ClickHouse type — bypasses primitive mapping",
    )
    codec: str | None = Field(
        default=None,
        description="Explicit CODEC contents, emitted verbatim - required to set a codec with ch_override",
    )
    max_dynamic_paths: int | None = Field(
        default=None,
        ge=1,
        description=(
            "JSON columns only: how many paths are stored as typed sub-columns. "
            "Paths beyond it spill into a slower shared map. Renders as "
            "JSON(max_dynamic_paths=N); ClickHouse's own default is 1024."
        ),
    )

    @field_validator("attribute", mode="before")
    @classmethod
    def _coerce_attribute(cls, v: Any) -> list[str]:
        """Accept a single string as a one-element list."""
        if isinstance(v, str):
            return [v]
        if v is None:
            return []
        return list(v)

    def validate_against_registry(self, registry: Any) -> list[str]:
        """Validate this column against a TypeRegistry.

        Returns a list of error messages (empty if valid).
        """
        errors: list[str] = []

        # Validate primitive
        if self.type not in registry.primitives and not self.ch_override:
            errors.append(f"Column {self.name!r}: unknown primitive {self.type!r}")
            return errors  # Can't validate further

        # Validate use_case↔primitive
        if self.use_case:
            try:
                registry.validate_use_case(self.type, self.use_case)
            except Exception as e:
                errors.append(f"Column {self.name!r}: {e}")

        # Validate attribute↔primitive
        for attr in self.attribute:
            try:
                registry.validate_attribute(self.type, attr)
            except Exception as e:
                errors.append(f"Column {self.name!r}: {e}")

        # Validate ch_override
        if self.ch_override:
            try:
                registry.validate_ch_override(self.ch_override)
            except Exception as e:
                errors.append(f"Column {self.name!r}: {e}")

        return errors


# ---------------------------------------------------------------------------
# Source Sub-Models
# ---------------------------------------------------------------------------


class SourceHeader(BaseModel):
    """Common schema header configuration."""

    # Must name a file in dfe-schemas common-header/; nothing normalises it.
    type: str = Field(
        default="timeseries",
        description="Profile name (timeseries, minimal, passthrough)",
    )
    version: str = Field(
        default="1.0.0",
        description="Common header version (semver)",
    )


class SourceMatch(BaseModel):
    """Receiver match rule — how the receiver identifies this source."""

    field: str = Field(
        ...,
        description=(
            "Field to match on: a dotted path into the record as it arrives "
            "(e.g. 'tags.collector.type', or a bare '_source'). The receiver "
            "matches the JSON payload, not a ClickHouse column, so a '_json.' "
            "prefix would be read as a first segment no record has."
        ),
    )
    operator: SourceMatchOperator = Field(
        default="equals",
        description=(
            "How to compare ``field`` to ``value``: equals (default), exists, "
            "includes, starts_with, ends_with, not_equals, always. ``always`` "
            "matches every record and is reserved for the ``default`` source"
        ),
    )
    value: str = Field(
        default="",
        description="Operand for the operator (not used by ``exists`` or ``always``)",
    )

    @model_validator(mode="after")
    def _validate_value_for_operator(self) -> SourceMatch:
        if self.operator in OPERATORS_WITHOUT_OPERAND:
            return self
        if not self.value.strip():
            raise ValueError(f"match.value is required when operator is {self.operator!r}")
        return self


class SourceSchema(BaseModel):
    """Schema configuration for the source's ClickHouse table."""

    meta_schema: str | None = Field(
        default=None,
        description="Base field definitions (YAML file reference)",
    )
    meta_schema_version: str | None = Field(
        default=None,
        description="Meta schema version (semver)",
    )
    derived_schema: str | None = Field(
        default=None,
        description="Source-specific field overrides (optional YAML reference)",
    )
    additional_fields: str | None = Field(
        default=None,
        description="Extra fields/indexes (optional YAML reference)",
    )
    ttl_days: int | None = Field(
        default=None,
        description="Data retention in days",
    )
    engine: str = Field(
        default="",
        description=(
            "MergeTree-family engine VARIANT, optionally parameterised - e.g. "
            "MergeTree, ReplacingMergeTree, ReplacingMergeTree(version_col), "
            "SummingMergeTree(a, b). Declare the base variant only: the topology "
            "(single vs Replicated/Shared/Cloud) is resolved at DDL time, so do NOT "
            "prefix Replicated/Shared here. Empty (the default) means inherit the "
            "deployment default (DFE_CLICKHOUSE_DEFAULT_ENGINE, itself MergeTree "
            "unless overridden)."
        ),
    )

    @field_validator("engine")
    @classmethod
    def _validate_engine(cls, v: str) -> str:
        # Empty = inherit the deployment default (resolved at DDL-build time); do
        # not gate it. Otherwise gate the VARIANT (the token before any "(") against
        # the single engine registry - the same allow-list the DDL generator uses,
        # so config and DDL cannot drift. Params inside the parens are the caller's.
        if not v:
            return v
        try:
            _ENGINE_REGISTRY.validate(v)
        except InvalidEngineError as e:
            raise ValueError(str(e)) from e
        return v


class SourceTransform(BaseModel):
    """Transform stage configuration: the engine is one of the catalogue's transform apps."""

    engine: str = Field(
        ...,
        description="Transform engine - a catalogued transform app by engine name (e.g. vrl, vector)",
    )
    variant: str | None = Field(
        default=None,
        description=(
            "The compiled-in program this instance runs, where the app offers a "
            "catalogue of them (dfe-transform-elastic selects one by source.name). "
            "None for an app whose program is the authored files it is given"
        ),
    )
    config_file: str | None = Field(default=None, description="Path to engine-specific config")
    env: dict[str, str] = Field(default_factory=dict, description="Per-transform ENV overrides")
    files: list[str] = Field(default_factory=list, description="Enrichment files (CSV, MMDB)")

    @field_validator("engine")
    @classmethod
    def _validate_engine(cls, v: str) -> str:
        # The catalogue (dfe-infra apps.yaml) is the only list of transform apps
        # that exist, so the accepted engines are read from it, never written here.
        # Imported at call time: appmgmt's package init reaches this module through
        # helm.compiler -> source.registry, so a top-level import is a cycle.
        from dfe_engine.appmgmt.catalogue import transform_engines

        valid = transform_engines()
        if v not in valid:
            raise ValueError(f"Invalid transform engine {v!r}. Valid: {', '.join(sorted(valid))}")
        return v


SourceOrigin = Literal["receiver", "fetcher"]
"""How a source's data enters the platform.

``receiver``: the always-present receiver pool identifies it by ``match``.
``fetcher``: one dfe-fetcher deployment, named for the source, polls it in.
"""

# Literal takes no constant, so DEFAULT_LANDING_LABEL is restated here and tested.
FetcherTopic = Literal["own", "main"]

TOPIC_LAND_SUFFIX = "_land"
TOPIC_LOAD_SUFFIX = "_load"


def landing_topic(source_name: str) -> str:
    """The topic a source's records arrive on, before any transform."""
    return f"{source_name}{TOPIC_LAND_SUFFIX}"


def transformed_topic(source_name: str) -> str:
    """The topic a source's transform writes, and the loader then reads."""
    return f"{source_name}{TOPIC_LOAD_SUFFIX}"


SOURCE_LABEL_FIELD = "_source"
"""The field every producer stamps with the label a record belongs to.

The receiver, the fetcher and each transform all read and write it, so a rule
that tests which source a record is has this as its field.
"""

DEFAULT_LANDING_LABEL = "main"
"""The ``_source`` label of the platform's landing table, ``main``.

Also the reserved name of the source that defines the main flow: it is a normal
source whose match rule is ``operator: always``, so an unmatched record follows
the same compiled path as any other. Every producer's fallback label, every
landing topic stem and the loader's fallback table resolve from this one name.
"""

# Keys the engine sets on the compiled fetcher stanza; a source may not carry them.
_FETCHER_ENGINE_KEYS = frozenset({"enabled", "topic"})

# The source YAML is committed to git, so a value under any of these must be an
# ``env:`` or ``vault:`` reference the fetcher resolves at runtime.
_CREDENTIAL_KEYS = frozenset(
    {
        "api_key",
        "access_key_id",
        "secret_access_key",
        "account_key",
        "integration_key",
        "secret_key",
        "private_key",
        "client_secret",
        "token",
    }
)
_CREDENTIAL_FRAGMENTS = ("secret", "token", "password")
_CREDENTIAL_REFERENCE_PREFIXES = ("env:", "vault:")


def _is_credential_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _CREDENTIAL_KEYS or any(f in lowered for f in _CREDENTIAL_FRAGMENTS)


def _find_plaintext_credential(value: Any, path: str = "") -> str | None:
    """The dot-path of the first credential-shaped key holding a literal, else None."""
    if isinstance(value, dict):
        for key, inner in value.items():
            here = f"{path}.{key}" if path else str(key)
            if (
                _is_credential_key(str(key))
                and isinstance(inner, str)
                and not inner.startswith(_CREDENTIAL_REFERENCE_PREFIXES)
            ):
                return here
            found = _find_plaintext_credential(inner, here)
            if found:
                return found
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            found = _find_plaintext_credential(inner, f"{path}[{index}]")
            if found:
                return found
    return None


class FetcherRoute(BaseModel):
    """One fetched record family this fetcher hands to a DIFFERENT source's landing.

    A fetcher polls one upstream but can pull several record shapes off it. A route
    sends the ones that match somewhere other than the owning source's landing, so
    they get that source's table and transform instead.
    """

    match: SourceMatch = Field(..., description="Which fetched records take this route")
    source: str = Field(..., description="The source whose landing the matched records go to")

    @field_validator("source")
    @classmethod
    def _validate_source(cls, v: str) -> str:
        return validate_source_name(v)


class SourceFetcher(BaseModel):
    """Fetcher-based origin: one dfe-fetcher deployment, named for the source.

    ``config`` is the fetcher's own per-type stanza, carried verbatim into the
    deployed instance under ``config.sources.<source_type>``. The engine owns
    ``enabled`` and ``topic`` on that stanza: ``topic`` selects whether records
    land on the source's own topic (and table) or on the shared ``main`` landing.
    """

    source_type: str = Field(
        ...,
        description=(
            "A dfe-fetcher source family (aws, okta, crates_io, ...); the deployed app "
            "manifest lists the accepted values"
        ),
    )
    topic: FetcherTopic = Field(
        default="own",
        description=(
            "own: records land on this source's topic and table; main: they land on "
            "the shared main topic and table"
        ),
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The fetcher's per-type stanza (services, connections, interval_secs, "
            "filter, credential references, ...). Credentials must be env: or vault: "
            "references"
        ),
    )
    routes: list[FetcherRoute] = Field(
        default_factory=list,
        description=(
            "Send matching fetched records to another source's landing instead of "
            "this one's. The named source must exist, which is checked when the "
            "fetcher instance is compiled, not here"
        ),
    )

    @field_validator("source_type")
    @classmethod
    def _validate_source_type(cls, v: str) -> str:
        # The manifest (dfe-infra apps.yaml) lists the fetcher's source families;
        # a manifest that declares none leaves the value unchecked. Imported at call
        # time: appmgmt reaches this module through helm.compiler -> source.registry.
        from dfe_engine.appmgmt.catalogue import source_types

        valid = source_types()
        if valid and v not in valid:
            raise ValueError(
                f"Unknown fetcher source_type {v!r}. Valid: {', '.join(sorted(valid))}"
            )
        return v

    @field_validator("config")
    @classmethod
    def _validate_config(cls, v: dict[str, Any]) -> dict[str, Any]:
        owned = _FETCHER_ENGINE_KEYS.intersection(v)
        if owned:
            raise ValueError(
                f"fetcher.config may not set {', '.join(sorted(owned))}: the engine derives "
                "them from the source"
            )
        plaintext = _find_plaintext_credential(v)
        if plaintext:
            raise ValueError(
                f"fetcher.config.{plaintext} holds a literal credential; the source is "
                "committed to git, so use an env: or vault: reference"
            )
        return v

    def landing_label(self, source_name: str) -> str:
        """The ``_source`` label and topic stem this source's records carry."""
        return source_name if self.topic == "own" else DEFAULT_LANDING_LABEL


class SourceView(BaseModel):
    """A naming-standard view this source exposes (sigma, ecs, cim, ocsf).

    Collapses the former ``mapping_standards`` + ``sigma`` + ``field_mappings``
    trio into one generic entry per standard. Inline ``custom_mappings`` WIN
    over the registry ``field_map`` when both are present.
    """

    standard: str = Field(..., description="Naming standard (sigma, ecs, cim, ocsf)")
    field_map: str | None = Field(
        default=None,
        description="FieldMap registry path for this view (optional)",
    )
    custom_mappings: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Inline per-source field overrides (standard_field: column_name); "
            "wins over the registry field_map"
        ),
    )
    # Sigma-only logsource binding for rule->source propagation. When set,
    # propagation only binds a rule to this source if the rule's category/service
    # also match; None matches ANY (so a bare-taxonomy source keeps the
    # product-only behaviour). Ignored for non-sigma standards.
    taxonomy: str | None = Field(
        default=None, description="Sigma logsource product this source serves (sigma views only)"
    )
    category: str | None = Field(
        default=None, description="Sigma logsource category this source serves (None = any)"
    )
    service: str | None = Field(
        default=None, description="Sigma logsource service this source serves (None = any)"
    )

    @field_validator("standard")
    @classmethod
    def _validate_standard(cls, v: str) -> str:
        # Lazy import: the fieldmap package __init__ pulls schema_ddl which
        # imports this module (circular at import time, fine at validation time).
        from dfe_engine.fieldmap.models import KNOWN_STANDARDS

        normalized = v.lower()
        if normalized not in KNOWN_STANDARDS:
            raise ValueError(
                f"Unknown view standard {v!r}. Valid: {', '.join(sorted(KNOWN_STANDARDS))}"
            )
        return normalized


_DEFAULT_SOURCE_VERSION = "1.0.0"
_FORBIDDEN_WRITE_KEYS = frozenset({"versions", "current", "deployed_version", "date_time"})
# 2.1 keys removed by the views clean break. Rejected LOUDLY on write: with
# extra="ignore" a pre-2.2 client would otherwise get a 200 while its mapping
# config silently vanished.
_REMOVED_WRITE_KEYS = frozenset({"sigma", "mapping_standards", "field_mappings"})
_VERSIONED_KEYS = (
    "header",
    "schema",
    "schema_config",
    "views",
    "fetcher",
    "match",
    "transform",
    "transport",
    "archive",
)

# ``variant`` selects which compiled-in program the transform runs; it changes
# what a record becomes, never the shape of the table it lands in.
_TRANSFORM_KEYS_WITHOUT_TABLE_EFFECT = {"variant"}


class SourceVersion(BaseModel):
    """Versioned source configuration snapshot (schema, mappings, fetcher, etc.)."""

    model_config = ConfigDict(populate_by_name=True)

    date_time: str = Field(..., description="Version creation date (YYYY-MM-DD)")
    header: SourceHeader | None = Field(
        default=None,
        description="Common schema header configuration (optional; applied at DDL compose time)",
    )
    schema_config: SourceSchema | None = Field(
        default=None,
        description="Schema configuration (None when the version did not author one)",
        alias="schema",
    )
    views: list[SourceView] = Field(
        default_factory=list,
        description="Naming-standard views this version exposes (sigma, ecs, cim, ocsf)",
    )
    fetcher: SourceFetcher | None = Field(default=None, description="SaaS API fetcher (optional)")

    @field_validator("views")
    @classmethod
    def _views_one_per_standard(cls, v: list[SourceView]) -> list[SourceView]:
        # Duplicate standards would let view_for() (first wins) and the DDL
        # overlay (last wins) silently disagree - refuse them outright.
        seen: set[str] = set()
        for view in v:
            if view.standard in seen:
                raise ValueError(f"duplicate view for standard {view.standard!r}")
            seen.add(view.standard)
        return v

    match: SourceMatch | None = Field(
        default=None, description="Receiver match rule (receiver-based sources)"
    )
    transform: SourceTransform | None = Field(
        default=None, description="Transform stage (optional)"
    )
    transport: SourceTransport | None = Field(
        default=None,
        description=(
            "How this source's stages hand records on: bus (a broker holds them) "
            "or direct (point to point). None takes the deployment default"
        ),
    )
    archive: bool = Field(
        default=False,
        description=(
            "Keep a copy of every record as it arrived, before any transform. The "
            "archiver reads the landing topic, so this needs the bus transport"
        ),
    )

    @model_validator(mode="after")
    def _exactly_one_origin(self) -> SourceVersion:
        if (self.match is None) == (self.fetcher is None):
            raise ValueError(
                "a source is receiver-based (match) or fetcher-based (fetcher): set exactly one"
            )
        return self

    @property
    def origin(self) -> SourceOrigin:
        """Which way this version's data enters the platform."""
        return "fetcher" if self.fetcher is not None else "receiver"

    def effective_header(self) -> SourceHeader:
        """Header for runtime/DDL resolution, defaulting the profile when unauthored."""
        return self.header or SourceHeader()

    def view_for(self, standard: str) -> SourceView | None:
        """This version's view entry for *standard*, or None when not declared."""
        return next((v for v in self.views if v.standard == standard), None)

    def effective_schema(self) -> SourceSchema:
        """Schema config for runtime/DDL resolution, defaulting to empty when unauthored."""
        return self.schema_config or SourceSchema()

    def to_yaml_dict(self) -> dict[str, Any]:
        """Serialize for YAML persistence under ``versions.<id>``."""
        raw = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        # Prune empty per-view containers (e.g. custom_mappings: {}) for clean YAML.
        for view in raw.get("views") or []:
            for key in list(view.keys()):
                if isinstance(view[key], (dict, list)) and not view[key]:
                    del view[key]
        for key in list(raw.keys()):
            if isinstance(raw[key], (dict, list)) and not raw[key]:
                del raw[key]
        return raw


def _derive_deployed_version(
    *, existing_deployed: str | None, snapshot: SourceVersion, version_id: str
) -> str | None:
    """Track current_version unless the version owns a meta_schema table to deploy."""
    if snapshot.effective_schema().meta_schema:
        return existing_deployed
    return version_id


def _next_major_semver(current: str) -> str:
    """Bump semver major (``1.2.3`` → ``2.0.0``)."""
    parts = current.strip().split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"Version {current!r} is not semver (expected x.x.x)")
    major = int(parts[0])
    return f"{major + 1}.0.0"


def next_major_source_version(
    existing_version_ids: dict[str, SourceVersion] | dict[str, Any],
) -> str:
    """Return the next major semver after the highest existing source version id."""
    if not existing_version_ids:
        return _DEFAULT_SOURCE_VERSION

    best = _DEFAULT_SOURCE_VERSION
    best_tuple = (1, 0, 0)
    for vid in existing_version_ids:
        parts = str(vid).split(".")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValueError(f"Existing source version id {vid!r} is not semver (expected x.x.x)")
        tup = (int(parts[0]), int(parts[1]), int(parts[2]))
        if tup > best_tuple:
            best_tuple = tup
            best = str(vid)

    return _next_major_semver(best)


class SourceWriteRequest(BaseModel):
    """Flat source definition for create/update API (no version tree)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    source: str | None = Field(
        default=None,
        description="Source name (_source label); required on create",
    )
    display_name: str | None = Field(default=None, description="Human-readable display name")
    description: str | None = Field(default=None, description="Source description")
    enabled: bool = Field(
        default=True,
        description="Compat boolean lifecycle (True -> active, False -> disabled); "
        "``state`` wins when both are sent",
    )
    state: SourceState | None = Field(
        default=None,
        description="Tri-state lifecycle (active | dormant | disabled); overrides ``enabled``",
    )
    match: SourceMatch | None = Field(
        default=None,
        description="Receiver match rule; required unless ``fetcher`` is set",
    )
    header: SourceHeader | None = Field(
        default=None,
        description="Common schema header configuration for this revision",
    )
    schema_config: SourceSchema | None = Field(
        default=None,
        description="Schema configuration for this revision",
        alias="schema",
    )
    transform: SourceTransform | None = Field(
        default=None, description="Transform stage (optional, top-level)"
    )
    fetcher: SourceFetcher | None = Field(
        default=None, description="Fetcher-based origin; required unless ``match`` is set"
    )
    transport: SourceTransport | None = Field(
        default=None,
        description="bus or direct; omitted takes the deployment default",
    )
    archive: bool = Field(
        default=False,
        description="Keep the raw record as it arrived; needs the bus transport",
    )
    views: list[SourceView] | None = Field(
        default=None,
        description="Naming-standard views for this revision (sigma, ecs, cim, ocsf)",
    )

    @model_validator(mode="after")
    def _exactly_one_origin(self) -> SourceWriteRequest:
        if (self.match is None) == (self.fetcher is None):
            raise ValueError(
                "a source is receiver-based (match) or fetcher-based (fetcher): set exactly one"
            )
        return self

    @model_validator(mode="before")
    @classmethod
    def _reject_version_tree_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            forbidden = _FORBIDDEN_WRITE_KEYS.intersection(data.keys())
            if forbidden:
                names = ", ".join(sorted(forbidden))
                raise ValueError(f"Fields not allowed on write: {names}")
            removed = _REMOVED_WRITE_KEYS.intersection(data.keys())
            if removed:
                names = ", ".join(sorted(removed))
                raise ValueError(
                    f"Fields removed in 2.2: {names} - declare naming-standard "
                    f"views via the 'views' list instead"
                )
        return data

    def effective_state(self, current: SourceState = "active") -> SourceState:
        """The tri-state this write requests (``state`` wins over ``enabled``).

        When the body sent NEITHER field, keep *current* - a PUT that only
        edits e.g. the description must not silently re-activate a dormant
        or disabled source. Creates pass the default (active).
        """
        if self.state is not None:
            return self.state
        if "enabled" in self.model_fields_set:
            return state_from_enabled(self.enabled)
        return current

    def to_version_snapshot(self) -> SourceVersion:
        """Build a new immutable version entry from this write payload."""
        return SourceVersion(
            date_time=date.today().isoformat(),
            header=self.header,
            schema_config=self.schema_config or SourceSchema(),
            views=self.views or [],
            fetcher=self.fetcher,
            match=self.match,
            transform=self.transform,
            transport=self.transport,
            archive=self.archive,
        )


def source_from_write(write: SourceWriteRequest, *, source_name: str) -> Source:
    """Create a new Source with initial version ``1.0.0`` from a flat write body."""
    version_id = _DEFAULT_SOURCE_VERSION
    snapshot = write.to_version_snapshot()
    payload: dict[str, Any] = {
        "source": source_name,
        "display_name": write.display_name,
        "description": write.description,
        "state": write.effective_state(),
        "deployed_version": None,
        "current": version_id,
        "versions": {version_id: snapshot.model_dump(mode="json", by_alias=True)},
    }
    return Source.model_validate(payload)


def _build_merged_version_snapshot(existing: Source, write: SourceWriteRequest) -> SourceVersion:
    """Build the version snapshot for a write, inheriting unset optional fields.

    ``transport`` and ``archive`` inherit on ABSENCE from the body, not on their
    value: ``transport: null`` is how a source is put back on the deployment
    default, and ``archive: false`` is how archiving is turned off, so a PUT that
    only edits the description must not read as either.
    """
    snapshot = write.to_version_snapshot()
    inherited: dict[str, Any] = {}
    if write.transform is None and existing.transform is not None:
        inherited["transform"] = existing.transform
    for field in ("transport", "archive"):
        if field not in write.model_fields_set:
            inherited[field] = getattr(existing, field)
    return snapshot.model_copy(update=inherited) if inherited else snapshot


def _schema_pin_for_bump(schema: SourceSchema) -> tuple[Any, ...]:
    """Schema references that change composed columns / deploy DDL."""
    return (
        schema.meta_schema,
        schema.meta_schema_version,
        schema.derived_schema,
        schema.additional_fields,
    )


def _transform_pin_for_bump(transform: SourceTransform | None) -> dict[str, Any] | None:
    """The transform fields that change the table, so a deployed edit needs a version."""
    if transform is None:
        return None
    return transform.model_dump(mode="json", exclude=_TRANSFORM_KEYS_WITHOUT_TABLE_EFFECT)


def source_version_bump_required(previous: SourceVersion, updated: SourceVersion) -> bool:
    """True when a deployed source needs a new major version id for this snapshot change.

    ``transport``, ``archive``, ``transform.variant`` and ``fetcher.routes`` are
    deliberately absent: they move records around, and a version id exists to
    pin the shape of the table those records land in.
    """
    prev_schema = previous.effective_schema()
    new_schema = updated.effective_schema()
    if _schema_pin_for_bump(prev_schema) != _schema_pin_for_bump(new_schema):
        return True
    # Any view change bumps: the standard set, a field_map pin, per-view
    # custom_mappings, or the sigma view's taxonomy/category/service.
    prev_views = [v.model_dump(mode="json") for v in previous.views]
    new_views = [v.model_dump(mode="json") for v in updated.views]
    if prev_views != new_views:
        return True
    return _transform_pin_for_bump(previous.transform) != _transform_pin_for_bump(updated.transform)


def draft_build_version_to_invalidate(existing: Source, updated: Source) -> str | None:
    """Return a version id whose persisted source-build should be dropped after an edit.

    Drops builds for draft work: versions that are not the live ``deployed_version``,
    including the pre-deploy case (``deployed_version`` is null). In-place bump-worthy
    schema/mapping changes on such a version invalidate its build. When a new major
    version is appended from the live deployed ``current``, the deployed version's
    build is kept.
    """
    deployed = updated.deployed_version
    current = updated.current

    if existing.current == current:
        if deployed is not None and deployed == current:
            return None
        previous = existing.versions[current]
        new_snap = updated.versions[current]
        if source_version_bump_required(previous, new_snap):
            return current
        return None

    bumped_from = existing.current
    if deployed is not None and deployed == bumped_from:
        return None
    if bumped_from not in updated.versions:
        return None
    previous = existing.versions[bumped_from]
    new_snap = updated.versions[bumped_from]
    if source_version_bump_required(previous, new_snap):
        return bumped_from
    return None


def apply_source_write_update(existing: Source, write: SourceWriteRequest) -> Source:
    """Persist a write: bump major version only when ``current`` is deployed and pins/mappings change."""
    snapshot = _build_merged_version_snapshot(existing, write)
    current_id = existing.current
    previous = existing.versions[current_id]

    append_version = (
        existing.deployed_version is not None
        and existing.deployed_version == current_id
        and source_version_bump_required(previous, snapshot)
    )

    merged_versions = dict(existing.versions)
    if append_version:
        new_version_id = next_major_source_version(existing.versions)
        if new_version_id in existing.versions:
            raise ValueError(f"Refusing to overwrite existing version {new_version_id!r}")
        merged_versions[new_version_id] = snapshot
        target_current = new_version_id
    else:
        merged_versions[current_id] = snapshot
        target_current = current_id

    payload: dict[str, Any] = {
        "source": existing.source,
        "display_name": write.display_name
        if write.display_name is not None
        else existing.display_name,
        "description": write.description if write.description is not None else existing.description,
        "state": write.effective_state(existing.state),
        "deployed_version": existing.deployed_version,
        "current": target_current,
        "versions": {
            vid: ver.model_dump(mode="json", by_alias=True) for vid, ver in merged_versions.items()
        },
    }
    return Source.model_validate(payload)


# ---------------------------------------------------------------------------
# Source — Top-Level Model
# ---------------------------------------------------------------------------


class Source(BaseModel):
    """The top-level data entity in the DFE platform.

    A Source represents a distinct data stream entering the platform.
    Everything flows from the _source label.

    See docs/data-plane/source.md for the full specification.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    source: str = Field(..., description="The _source label — immutable identifier")
    display_name: str | None = Field(default=None, description="Human-readable display name")
    description: str | None = Field(default=None, description="Source description")
    state: SourceState = Field(
        default="active",
        description="Lifecycle state: active (all on), dormant (schema pre-positioned, "
        "routing/transform off), disabled (all off, table reclaimed)",
    )
    deployed_version: str | None = Field(
        default=None,
        description="Version deployed to ClickHouse / runtime (null until first deploy)",
    )
    current: str = Field(
        default=_DEFAULT_SOURCE_VERSION,
        description="Working version (latest definition)",
    )
    versions: dict[str, SourceVersion] = Field(
        default_factory=dict,
        description="Version id → configuration snapshot",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_or_versioned(cls, data: Any) -> Any:
        """Accept legacy flat YAML/API bodies and normalize to the version tree."""
        if not isinstance(data, dict):
            return data

        data = dict(data)
        # Legacy boolean lifecycle: map `enabled` onto the tri-state when the
        # input does not declare `state` (enabled==True -> active, False ->
        # disabled). `state` wins when both are present.
        enabled_raw = data.pop("enabled", None)
        if "state" not in data and enabled_raw is not None:
            data["state"] = "active" if enabled_raw else "disabled"

        if data.get("versions"):
            top_match = data.pop("match", None)
            top_transform = data.pop("transform", None)
            if top_match is not None or top_transform is not None:
                for ver in data["versions"].values():
                    if not isinstance(ver, dict):
                        continue
                    if top_match is not None and "match" not in ver:
                        ver["match"] = top_match
                    if top_transform is not None and "transform" not in ver:
                        ver["transform"] = top_transform
            current = data.get("current") or data.get("deployed_version")
            if current and "current" not in data:
                data["current"] = current
            return data

        version_id = _DEFAULT_SOURCE_VERSION
        header_raw = data.pop("header", None)

        version_body: dict[str, Any] = {
            "date_time": data.pop("date_time", None) or date.today().isoformat()
        }
        if header_raw is not None:
            version_body["header"] = header_raw

        if "schema" in data:
            version_body["schema"] = data.pop("schema")
        elif "schema_config" in data:
            version_body["schema"] = data.pop("schema_config")

        for key in _VERSIONED_KEYS:
            if key in ("header", "schema", "schema_config"):
                continue
            if key in data:
                version_body[key] = data.pop(key)

        data.setdefault("current", version_id)
        data["versions"] = {version_id: version_body}
        return data

    @field_validator("source")
    @classmethod
    def _validate_source_name(cls, v: str) -> str:
        """Enforce _source naming rules."""
        return validate_source_name(v)

    @model_validator(mode="after")
    def _validate_versions_and_display_name(self) -> Source:
        """Default display_name and ensure version pointers are valid."""
        if self.display_name is None:
            # The word separator in a source name is the hyphen, so that is what
            # becomes a space when a display name is derived rather than given.
            self.display_name = self.source.replace("-", " ").title()

        if not self.versions:
            raise ValueError("versions must contain at least one version entry")

        if self.current not in self.versions:
            raise ValueError(f"current version '{self.current}' is not defined in versions")
        if self.deployed_version is not None and self.deployed_version not in self.versions:
            raise ValueError(
                f"deployed_version '{self.deployed_version}' is not defined in versions"
            )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def enabled(self) -> bool:
        """Compat accessor over the tri-state: enabled == (state == active).

        Serialized on API responses so boolean consumers keep working.
        """
        return self.state == "active"

    def runtime_version_id(self) -> str:
        """Version used for runtime accessors when ``deployed_version`` is unset."""
        return self.deployed_version or self.current

    def version(self, version_id: str | None = None) -> SourceVersion:
        """Return a specific version snapshot (defaults to deployed, else current)."""
        vid = version_id or self.runtime_version_id()
        try:
            return self.versions[vid]
        except KeyError as e:
            raise ValueError(f"Source version '{vid}' is not defined") from e

    @property
    def header(self) -> SourceHeader:
        """Deployed version header (legacy accessor; defaults the profile when unauthored)."""
        return self.version().effective_header()

    @property
    def schema_config(self) -> SourceSchema:
        """Deployed version schema config (legacy accessor; empty default when unauthored)."""
        return self.version().effective_schema()

    @property
    def views(self) -> list[SourceView]:
        """Deployed version naming-standard views (legacy accessor)."""
        return self.version().views

    def view_for(self, standard: str) -> SourceView | None:
        """Deployed version's view entry for *standard*, or None when not declared."""
        return self.version().view_for(standard)

    @property
    def fetcher(self) -> SourceFetcher | None:
        """Deployed version fetcher config (legacy accessor)."""
        return self.version().fetcher

    @computed_field  # type: ignore[prop-decorator]
    @property
    def match(self) -> SourceMatch | None:
        """Receiver match rule on the deployed version (serialized for API compat)."""
        return self.version().match

    @computed_field  # type: ignore[prop-decorator]
    @property
    def transform(self) -> SourceTransform | None:
        """Transform config on the deployed version (serialized for API compat)."""
        return self.version().transform

    @computed_field  # type: ignore[prop-decorator]
    @property
    def origin(self) -> SourceOrigin:
        """How the deployed version's data enters: receiver match or a fetcher."""
        return self.version().origin

    @computed_field  # type: ignore[prop-decorator]
    @property
    def transport(self) -> SourceTransport | None:
        """Declared transport on the deployed version (None = the deployment default)."""
        return self.version().transport

    @computed_field  # type: ignore[prop-decorator]
    @property
    def archive(self) -> bool:
        """Whether the deployed version keeps the raw record."""
        return self.version().archive

    def landing_label(self) -> str:
        """The ``_source`` label this source's records carry when they land."""
        fetcher = self.fetcher
        if fetcher is None:
            return self.source
        return fetcher.landing_label(self.source)

    # -----------------------------------------------------------------
    # Derived properties
    # -----------------------------------------------------------------

    @property
    def topic_land(self) -> str:
        """Kafka topic for raw data from receiver."""
        return landing_topic(self.source)

    @property
    def topic_load(self) -> str | None:
        """Kafka topic for transformed data (only if transform exists)."""
        return transformed_topic(self.source) if self.transform else None

    @property
    def table_name(self) -> str:
        """ClickHouse table name (same as _source label)."""
        return self.source

    def to_yaml_dict(self) -> dict[str, Any]:
        """Serialize to a dict suitable for YAML output.

        Top-level identity and routing; versioned config under ``versions``.
        """
        data: dict[str, Any] = {
            "source": self.source,
            "display_name": self.display_name,
            "state": self.state,
            "current": self.current,
            "versions": {vid: ver.to_yaml_dict() for vid, ver in sorted(self.versions.items())},
        }
        if self.deployed_version is not None:
            data["deployed_version"] = self.deployed_version
        if self.description is not None:
            data["description"] = self.description
        return data


# ── Version GET response (API) ───────────────────────────────


class SourceVersionGetResponse(BaseModel):
    """Source payload for a single requested version (mirrors ``MetaSchemaGetResponse``)."""

    model_config = ConfigDict(populate_by_name=True)

    source: str = Field(..., description="Source name (_source label)")
    display_name: str | None = Field(default=None, description="Human-readable display name")
    description: str | None = Field(default=None, description="Source description")
    state: SourceState = Field(default="active", description="Lifecycle state")
    enabled: bool = Field(default=True, description="Compat accessor (state == active)")
    current: str = Field(..., description="Working version id")
    deployed_version: str | None = Field(
        default=None,
        description="Version deployed to ClickHouse / runtime (null until first deploy)",
    )
    selected: str = Field(..., description="Version id requested in the URL path")
    versions: list[str] = Field(..., description="All version ids defined on this source")
    previous_deployed_versions: list[str] = Field(
        default_factory=list,
        description=(
            "Version ids with a successful deploy in source-deploys history, "
            "excluding the live deployed_version"
        ),
    )
    version: SourceVersion = Field(
        ..., description="Immutable configuration snapshot for ``selected``"
    )


# ── List / summary response models (API) ─────────────────────


class SourceSummaryObject(BaseModel):
    """Summary row for paginated source list (mirrors ``SchemaSummaryObject``)."""

    name: str = Field(description="Source name (_source label)")
    display_name: str | None = Field(default=None, description="Human-readable display name")
    description: str | None = Field(default=None, description="Source description")
    state: SourceState = Field(default="active", description="Lifecycle state")
    enabled: bool = Field(default=True, description="Compat accessor (state == active)")
    current: str = Field(description="Working version id")
    deployed_version: str | None = Field(
        default=None,
        description="Version deployed to ClickHouse / runtime (null until first deploy)",
    )
    versions: list[str] = Field(description="All defined version ids")
    updated_at: str = Field(default="", description="Last updated timestamp (ISO 8601)")
    header_type: str | None = Field(
        default=None,
        description="Common header profile type (deployed version)",
    )
    has_transform: bool = Field(
        default=False, description="Whether a transform stage is configured"
    )
    has_fetcher: bool = Field(default=False, description="Whether a fetcher is configured")
    origin: SourceOrigin = Field(
        default="receiver", description="How data enters: receiver match or a fetcher"
    )
    views: list[str] = Field(
        default_factory=list,
        description="Naming-standard views on the deployed version (standard names)",
    )


PathTree[SourceSummaryObject].model_rebuild()
SourceSummaryTree = PathTree[SourceSummaryObject]
"""Source list tree grouped by path segments derived from the source name."""


class PaginatedSourceSummaryResponse(
    PaginatedResponseWithObjects[SourceSummaryObject, SourceSummaryTree]
):
    """Source list: path tree in ``objects`` plus paginated ``items``."""

    @classmethod
    def from_summaries(
        cls,
        summaries: list[SourceSummaryObject],
        page: int,
        per_page: int,
    ) -> PaginatedSourceSummaryResponse:
        return cls.with_objects(
            summaries,
            page,
            per_page,
            SourceSummaryTree.from_paths(
                summaries,
                path=lambda obj: obj.name,
            ),
        )
