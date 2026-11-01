"""Configuration model for dfe-receiver.

Mirrors the Rust config structs in dfe-receiver/src/config/mod.rs.
All defaults match the Rust `impl Default` values exactly.
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from dfe_engine.orgs.models import DEFAULT_ORG_ID
from dfe_engine.services.models.base import BaseServiceConfig
from dfe_engine.services.models.common import (
    AcknowledgementsConfig,
    DlqConfig,
    KafkaTlsConfig,
    SaslConfig,
)
from dfe_engine.source.models import DEFAULT_LANDING_LABEL

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class TlsConfig(BaseModel):
    """TLS/mTLS configuration for the HTTP server."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    cert_file: str | None = None
    key_file: str | None = None
    ca_file: str | None = None
    client_auth: str = Field(default="none", description="none, optional, required")
    cert_secret: str | None = None
    key_secret: str | None = None
    ca_secret: str | None = None
    refresh_interval_secs: int = Field(default=3600, ge=0)

    @field_validator("client_auth")
    @classmethod
    def validate_client_auth(cls, v: str) -> str:
        """Refuse a client_auth other than none, optional or required."""
        allowed = {"none", "optional", "required"}
        if v.lower() not in allowed:
            msg = f"Invalid client_auth: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v


class AcceptedHeader(BaseModel):
    """An accepted authentication header definition, whose values are the credential."""

    model_config = ConfigDict(extra="forbid")

    name: str
    values: list[SecretStr] = []


class BearerConfig(BaseModel):
    """Bearer token authentication configuration."""

    model_config = ConfigDict(extra="forbid")

    tokens: list[SecretStr] = []
    secret_source: str | None = None
    refresh_interval_secs: int = Field(default=300, ge=0)


class AuthConfig(BaseModel):
    """Authentication configuration for one listener."""

    model_config = ConfigDict(extra="forbid")

    mode: str = Field(default="none", description="none, header, bearer, mtls, both")
    accepted_headers: list[AcceptedHeader] = Field(default_factory=list)
    bearer: BearerConfig = Field(default_factory=BearerConfig)
    header_name: str = ""
    header_values: list[SecretStr] = []

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        """Refuse an auth mode other than none, header, bearer, mtls or both."""
        allowed = {"none", "header", "bearer", "mtls", "both"}
        if v.lower() not in allowed:
            msg = f"Invalid auth mode: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v


class ServerConfig(BaseModel):
    """HTTP server configuration."""

    model_config = ConfigDict(extra="forbid")

    bind_address: str = "0.0.0.0:8080"
    max_body_size: int = Field(default=10 * 1024 * 1024, ge=0, description="bytes")
    request_timeout_ms: int = Field(default=30_000, ge=0)
    tls: TlsConfig = Field(default_factory=TlsConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    acknowledgements: AcknowledgementsConfig = Field(default_factory=AcknowledgementsConfig)


# ---------------------------------------------------------------------------
# gRPC
# ---------------------------------------------------------------------------


class GrpcConfig(BaseModel):
    """gRPC server configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    bind_address: str = "0.0.0.0:6000"
    auth: AuthConfig = Field(default_factory=AuthConfig)
    max_message_size: int = Field(
        default=16 * 1024 * 1024, ge=0, description="Largest decoded push accepted, in bytes"
    )
    acknowledgements: AcknowledgementsConfig = Field(default_factory=AcknowledgementsConfig)


class BearerListenerConfig(BaseModel):
    """A listener the engine wires bearer auth on, modelled as far as ``enabled`` and ``auth``.

    Its other keys pass through for dfe-receiver to validate.
    """

    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    auth: AuthConfig = Field(default_factory=AuthConfig)


class OtlpConfig(BearerListenerConfig):
    """The OTLP gRPC and HTTP listeners."""


class SplunkHecConfig(BearerListenerConfig):
    """The Splunk HTTP Event Collector listener."""


class PrometheusRwConfig(BearerListenerConfig):
    """The Prometheus Remote Write listener."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ValidationConfig(BaseModel):
    """Input validation configuration."""

    model_config = ConfigDict(extra="forbid")

    require_json: bool = True
    required_fields: list[str] = []
    dlq_on_invalid: bool = True


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

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


class OrgBinding(BaseModel):
    """dfe-receiver ``OrgBinding``: the orgs one principal's records may land in."""

    model_config = ConfigDict(extra="forbid")

    orgs: list[str] = Field(min_length=1, description="Tenant ids its records may land in")
    default_org: str | None = Field(
        default=None, description="The tenant a record takes when no rule picks one of orgs"
    )

    @model_validator(mode="after")
    def _default_in_orgs(self) -> Self:
        """Refuse a default outside the binding, which the receiver refuses at load."""
        if self.default_org is not None and self.default_org not in self.orgs:
            raise ValueError(f"default_org {self.default_org!r} is not one of orgs")
        return self


class IngestBindingsConfig(BaseModel):
    """dfe-receiver ``IngestBindingsConfig``: the orgs each credential and listener may write.

    A credential or listener with no entry may land records in the default org alone.
    """

    model_config = ConfigDict(extra="forbid")

    credentials: dict[str, OrgBinding] = Field(
        default_factory=dict, description="By credential id, the id before ':' in a token entry"
    )
    listeners: dict[str, OrgBinding] = Field(
        default_factory=dict, description="By listener, for a request that names no credential"
    )


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


# ---------------------------------------------------------------------------
# Kafka (Receiver-specific: producer settings)
# ---------------------------------------------------------------------------


class ProducerConfig(BaseModel):
    """Kafka producer settings."""

    model_config = ConfigDict(extra="forbid")

    batch_size: int = Field(default=8 * 1024 * 1024, gt=0, description="bytes")
    batch_messages: int = Field(default=10_000, gt=0)
    linger_ms: int = Field(default=20, ge=0)
    compression: str = "lz4"
    acks: str = Field(default="all", description="0, 1, or all")
    retries: int = Field(default=5, ge=0)

    @field_validator("compression")
    @classmethod
    def validate_compression(cls, v: str) -> str:
        """Refuse a compression codec other than none, gzip, snappy, lz4 or zstd."""
        allowed = {"none", "gzip", "snappy", "lz4", "zstd"}
        if v.lower() not in allowed:
            msg = f"Invalid compression: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v


class ReceiverKafkaConfig(BaseModel):
    """Kafka producer configuration for the receiver."""

    model_config = ConfigDict(extra="forbid")

    brokers: list[str] = []
    client_id: str = "dfe-receiver"
    sasl: SaslConfig | None = None
    tls: KafkaTlsConfig = Field(default_factory=KafkaTlsConfig)
    producer: ProducerConfig = Field(default_factory=ProducerConfig)


# ---------------------------------------------------------------------------
# Loader connection
# ---------------------------------------------------------------------------


def _loader_push_address() -> str:
    """Where the app manifest puts the loader's push listener, as ``host:port``."""
    from dfe_engine.appmgmt import catalogue

    return catalogue.push_address(catalogue.descriptor("dfe-loader"))


class LoaderConnectionConfig(BaseModel):
    """Connection configuration for dfe-loader transport.

    The transports are the receiver's own (``LoaderConfig`` in its
    ``src/config/mod.rs``): ``grpc`` is the direct path this block exists for.
    """

    model_config = ConfigDict(extra="forbid")

    address: str = Field(
        default_factory=_loader_push_address,
        description="The loader's push listener on its own Service, as the app manifest declares it",
    )
    transport: str = Field(default="kafka", description="kafka or grpc")
    # Inside the 25 s a listener holds its answer, so a delivery settles before it.
    timeout_ms: int = Field(
        default=20_000,
        ge=0,
        description="Per-RPC deadline for the receiver's gRPC loader client (0 = none)",
    )
    grpc_endpoint: str | None = Field(
        default=None,
        description="gRPC endpoint URI; the receiver derives http://{address} when unset",
    )

    @field_validator("transport")
    @classmethod
    def validate_transport(cls, v: str) -> str:
        # `memory` discards every record and the receiver refuses it at startup,
        # so writing it here would produce a config that cannot boot.
        """Refuse a transport other than kafka or grpc."""
        allowed = {"kafka", "grpc"}
        if v.lower() not in allowed:
            msg = f"Invalid transport: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v


# ---------------------------------------------------------------------------
# Buffer
# ---------------------------------------------------------------------------


class ReceiverBufferConfig(BaseModel):
    """Buffer and memory configuration for the receiver."""

    model_config = ConfigDict(extra="forbid")

    memory_limit: int = Field(default=0, ge=0, description="0 = auto-detect")
    pressure_threshold: float = Field(default=0.8, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


class ReceiverConfig(BaseServiceConfig):
    """Complete configuration for dfe-receiver.

    Mirrors the Rust Config struct in dfe-receiver/src/config/mod.rs.
    """

    server: ServerConfig = Field(default_factory=ServerConfig)
    grpc: GrpcConfig = Field(default_factory=GrpcConfig)
    otlp: OtlpConfig = Field(default_factory=OtlpConfig)
    splunk_hec: SplunkHecConfig = Field(default_factory=SplunkHecConfig)
    prometheus_rw: PrometheusRwConfig = Field(default_factory=PrometheusRwConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    routing: ReceiverRoutingConfig = Field(default_factory=ReceiverRoutingConfig)
    rules: RuleSet = Field(default_factory=RuleSet)
    ingest_bindings: IngestBindingsConfig = Field(default_factory=IngestBindingsConfig)
    destinations: DestinationsConfig = Field(default_factory=DestinationsConfig)
    kafka: ReceiverKafkaConfig = Field(default_factory=ReceiverKafkaConfig)
    loader: LoaderConnectionConfig = Field(default_factory=LoaderConnectionConfig)
    buffer: ReceiverBufferConfig = Field(default_factory=ReceiverBufferConfig)
