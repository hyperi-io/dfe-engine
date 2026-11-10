#  Project:      dfe-engine
#  File:         settings.py
#  Purpose:      Centralized configuration management with Pydantic
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""DFE engine settings.

``load_settings`` layers defaults.yaml, an optional config file and the environment, the
environment winning. ``_ENV_FIELDS`` lists every environment variable that sets a field, and
``DFE_CONFIG_DIR`` fills each registry directory the environment does not name. The cascade is
the engine's own, not scalo's: moving onto scalo's is #600.
"""

import contextlib
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from dfe_schemas.clickhouse import platform_override
from pydantic import BaseModel, Field, field_validator, model_validator

from .names import MAX_NAME_LENGTH
from .orgs.models import ORG_NAME_PATTERN, TenantId
from .transport import SourceTransport
from .yaml_utils import yaml_load


class ClickHouseCloudSettings(BaseModel):
    """ClickHouse Cloud service-lifecycle config (CONTROL PLANE; opt-in).

    Separate from the SQL connection (the regular ``clickhouse.*`` block pointed
    at a ``*.clickhouse.cloud`` host). This block is the CONTROL-PLANE management
    API (``api.clickhouse.cloud``) used to see / start / stop the service - a
    BILLABLE lever, off by default. The api key needs only service read +
    state-management (a SERVICE-SCOPED key, NOT an org-admin key).
    """

    api_key_id: str = Field(default="", description="CH Cloud mgmt API key id (control plane).")
    api_key_secret: str = Field(
        default="", description="CH Cloud mgmt API key secret (control plane)."
    )
    api_base: str = Field(
        default="https://api.clickhouse.cloud/v1", description="CH Cloud management API base URL."
    )
    organization_id: str = Field(
        default="", description="CH Cloud org id (empty = auto-discover the first org for the key)."
    )
    service_id: str = Field(
        default="", description="CH Cloud service UUID (takes precedence over service_name)."
    )
    service_name: str = Field(
        default="dfe", description="CH Cloud service name to select when service_id is empty."
    )
    autowake: bool = Field(
        default=False,
        description=(
            "Opt-in: on a CH connect failure, if the Cloud service is stopped/idle, START it "
            "(billable). OFF by default; requires the api key + a non-prod posture."
        ),
    )

    @property
    def configured(self) -> bool:
        """True when the control-plane creds are present (lifecycle usable)."""
        return bool(self.api_key_id and self.api_key_secret)


class ClickHouseResilienceSettings(BaseModel):
    """Reconnect-and-retry back-off + budget for the CH data-plane connection.

    Feeds scalo's :class:`~scalo.resilience.ResilienceConfig` (config-cascade - an
    operator widens the budget for a slow cold start without a code change, never a
    hardcoded call-site value). A CONNECTION outage backs off AND rebuilds the
    pooled client; a rate-limit (202) backs off WITHOUT reconnecting; a genuine
    query error surfaces immediately. Field names mirror ``ResilienceConfig`` so it
    is passed straight through. Off (``enabled=false``) restores the pre-resilience
    behaviour - every op runs once and raises on the first failure.
    """

    enabled: bool = Field(default=True, description="Master switch for CH reconnect-and-retry.")
    wait_initial: float = Field(default=0.5, description="First back-off, seconds.")
    wait_max: float = Field(default=10.0, description="Per-attempt back-off cap, seconds.")
    wait_multiplier: float = Field(default=2.0, description="Exponential back-off factor.")
    budget_seconds: float = Field(
        default=60.0, description="Backstop budget for a transient CH outage, seconds."
    )
    waking_budget_seconds: float = Field(
        default=300.0,
        description="Extended budget once a CH Cloud auto-wake is in flight, seconds.",
    )


class ClickHouseSettings(BaseModel):
    """ClickHouse connection settings."""

    host: str = Field(default="localhost")
    port: int = Field(
        default=8123,
        description="ClickHouse HTTP port: the client is clickhouse-connect over HTTP, so the native 9000 cannot work",
    )
    username: str = Field(default="default")
    password: str = Field(default="")
    database: str = Field(
        default="default",
        description="Database the client connection authenticates against (its default db)",
    )
    data_database: str = Field(
        default="dfe",
        description=(
            "THE DFE database, from DFE_ENGINE_CLICKHOUSE_DATA_DATABASE (default `dfe`). "
            "Everything DFE writes lives here: the landing table, per-source tables, "
            "hunt output, the OTel telemetry tables, and the engine's own state. One "
            "name, because org isolation is by ROW POLICY rather than by splitting "
            "databases (see governance.ch.models). Lets the connection authenticate "
            "against one database while DFE tables are qualified against another -- "
            "read it via `effective_data_database`, never directly."
        ),
    )
    default_table_profile: str = Field(
        default="timeseries",
        description="Schema profile the bootstrapped landing table is built from (DFE_ENGINE_DEFAULT_TABLE_PROFILE)",
    )
    bootstrap_tables: bool = Field(
        default=True,
        description=(
            "Apply the dfe-schemas manifest at startup (DFE_ENGINE_CLICKHOUSE_BOOTSTRAP_TABLES). "
            "Off means the engine reports the schema state as unknown and gates nothing; "
            "it does not bring a separate bootstrapper back."
        ),
    )
    bootstrap_wait_seconds: float = Field(
        default=180.0,
        ge=0.0,
        description=(
            "How long the schema phase keeps retrying an unreachable ClickHouse before "
            "reporting the pass failed (DFE_ENGINE_CLICKHOUSE_BOOTSTRAP_WAIT_SECONDS). The "
            "datastore can be in the same deploy wave as the engine, so not-up-yet and "
            "broken are different answers."
        ),
    )
    secure: bool = Field(default=True)
    verify: bool | None = Field(
        default=None,
        description=(
            "Verify the ClickHouse server certificate. None (default) follows the "
            "SCALO_TLS_VERIFY escape valve - i.e. verify ON unless the whole "
            "environment disables it; DFE_ENGINE_CLICKHOUSE_VERIFY overrides per-CH "
            "(set false for self-signed dev/test infra)."
        ),
    )
    ca_cert: str | None = Field(
        default=None,
        description="PEM CA file trusted to verify the ClickHouse server cert (internal CA); DFE_ENGINE_CLICKHOUSE_CA_CERT.",
    )
    connections_max: int = Field(default=300)
    platform: str = Field(
        default="auto",
        description=(
            "The ClickHouse platform schema DDL is rendered for (DFE_ENGINE_CLICKHOUSE_PLATFORM): "
            "auto, cloud or self_hosted. auto asks the server, then reads the hostname. "
            "cloud or self_hosted pins it, for a server the probe reads wrongly."
        ),
    )
    ddl_max_query_size: int = Field(
        default=16_777_216,
        ge=262_144,
        description=(
            "Ceiling in bytes on the max_query_size sent with a schema DDL statement "
            "(DFE_ENGINE_CLICKHOUSE_DDL_MAX_QUERY_SIZE). A statement over ClickHouse's 256 KiB "
            "default is sent with its own size plus a small margin; one that would need "
            "more than this is refused before it is sent."
        ),
    )
    # Default MergeTree-family VARIANT for tables that do not pin their own engine.
    # dfe-schemas adds the Replicated prefix every DFE table takes, and this decides
    # the family (MergeTree / ReplacingMergeTree / SummingMergeTree / ...). Must be a
    # variant the engine registry permits, and may be parameterised
    # (e.g. "ReplacingMergeTree(version)").
    default_engine: str = Field(default="MergeTree")
    # Retention every time-series table gets unless dfe-schemas or a source
    # declares its own; 0 disables the default.
    default_ttl_days: int = Field(default=90, ge=0)
    # What an unauthored source header inherits. A deploy-repo override replaces
    # these; the strings are SourceHeader's compiled-in default.
    default_header_type: str = Field(default="timeseries")
    default_header_version: str = Field(default="1.0.3")
    cloud: ClickHouseCloudSettings = Field(default_factory=ClickHouseCloudSettings)
    resilience: ClickHouseResilienceSettings = Field(default_factory=ClickHouseResilienceSettings)
    interactive_budget_seconds: float = Field(
        default=5.0,
        gt=0,
        description=(
            "Reconnect budget for a read a console request waits on, with no Cloud "
            "auto-wake, so a down server answers 503 within it rather than within "
            "resilience.budget_seconds."
        ),
    )

    @field_validator("platform")
    @classmethod
    def _platform_is_a_known_choice(cls, value: str) -> str:
        # dfe-schemas owns the choices, and refuses an unknown one naming them all.
        platform_override(value)
        return value.strip().lower()

    @property
    def effective_data_database(self) -> str:
        """Database to qualify DFE table references with.

        Returns ``data_database`` when set, else the connection ``database``.
        Use this anywhere a query names a DFE table as ``db.table`` (discovery,
        promotion, query-views, loader routing) so the connection's default
        database and the data tables' database can diverge.
        """
        return self.data_database or self.database


# Shared by HuntsSettings and the hunt runner compiler's default, so the two cannot drift.
MAX_DETECTIONS_PER_RUN = 1000
MAX_DETECTIONS_PER_RUN_CEILING = 10_000

# The safe horizon trails the replication delay by this much, for inserts not yet in a replica's queue.
SAFE_HORIZON_COMMIT_ALLOWANCE_SECONDS = 10
# The overlap is the only cover for a row the horizon cannot see, so it never drops below the allowance.
MIN_WINDOW_OVERLAP_SECONDS = SAFE_HORIZON_COMMIT_ALLOWANCE_SECONDS
WINDOW_OVERLAP_SECONDS = 120
SAFE_HORIZON_MAX_DEFERRAL_SECONDS = 300

# A plain database name, the only shape the rule guard compares a lookup's database against.
LOOKUP_DATABASE_PATTERN = r"[A-Za-z_][A-Za-z0-9_-]*"

# ClickHouse's own databases describe every org's tables and queries, so none is a lookup.
_CLICKHOUSE_CATALOGUES = frozenset({"system", "information_schema"})


class DetectionGuardSettings(BaseModel):
    """Alert-volume preview a rule gets when it is created or updated.

    The preview counts what the rule matches over a lookback window on its own
    source table, projects that to a day, and warns in bands. Every band warns,
    and none blocks the save. The 150 and 300 a day defaults are ISA-18.2's range
    for what one operator can handle.
    """

    enabled: bool = Field(
        default=True,
        description="Warn on a rule's measured alert volume. DFE_ENGINE_DETECTION_GUARD_ENABLED.",
    )
    preview_window_minutes: int = Field(
        default=60,
        gt=0,
        description=(
            "Lookback window the preview counts over, unless the request names its own. "
            "DFE_ENGINE_DETECTION_GUARD_PREVIEW_WINDOW_MINUTES."
        ),
    )
    preview_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        description=(
            "Longest the preview query may run before it is reported unfinished. "
            "DFE_ENGINE_DETECTION_GUARD_PREVIEW_TIMEOUT_SECONDS."
        ),
    )
    preview_max_rows: int = Field(
        default=50_000_000,
        gt=0,
        description=(
            "Most rows the preview may read before it is reported unfinished. "
            "DFE_ENGINE_DETECTION_GUARD_PREVIEW_MAX_ROWS."
        ),
    )
    warn_per_day: int = Field(
        default=150,
        gt=0,
        description=(
            "Projected alerts a day above which the rule gets guidance. "
            "DFE_ENGINE_DETECTION_GUARD_WARN_PER_DAY."
        ),
    )
    ack_per_day: int = Field(
        default=300,
        gt=0,
        description=(
            "Projected alerts a day above which the rule gets a warning. "
            "DFE_ENGINE_DETECTION_GUARD_ACK_PER_DAY."
        ),
    )
    block_per_day: int = Field(
        default=10_000,
        gt=0,
        description=(
            "Projected alerts a day above which the rule is called out as plainly bad. "
            "DFE_ENGINE_DETECTION_GUARD_BLOCK_PER_DAY."
        ),
    )
    warn_match_ratio: float = Field(
        default=0.10,
        gt=0,
        le=1,
        description=(
            "Share of the window's events a rule may match before it gets a note. "
            "DFE_ENGINE_DETECTION_GUARD_WARN_MATCH_RATIO."
        ),
    )
    block_match_ratio: float = Field(
        default=0.50,
        gt=0,
        le=1,
        description=(
            "Share of the window's events that makes a rule over ack_per_day plainly "
            "bad. DFE_ENGINE_DETECTION_GUARD_BLOCK_MATCH_RATIO."
        ),
    )
    min_rows_for_ratio: int = Field(
        default=1000,
        ge=0,
        description=(
            "Events the window must hold before the match share is judged at all. "
            "DFE_ENGINE_DETECTION_GUARD_MIN_ROWS_FOR_RATIO."
        ),
    )

    @model_validator(mode="after")
    def _bands_in_order(self) -> DetectionGuardSettings:
        if not self.warn_per_day < self.ack_per_day < self.block_per_day:
            raise ValueError(
                "detection guard bands must rise: warn_per_day "
                f"({self.warn_per_day}) < ack_per_day ({self.ack_per_day}) < "
                f"block_per_day ({self.block_per_day})"
            )
        if not self.warn_match_ratio < self.block_match_ratio:
            raise ValueError(
                "detection guard ratios must rise: warn_match_ratio "
                f"({self.warn_match_ratio}) < block_match_ratio ({self.block_match_ratio})"
            )
        return self


class HuntsSettings(BaseModel):
    """Hunt scheduler settings."""

    hunt_dir: str = Field(
        default="",
        description=(
            "The one directory holding hunt YAML, which the engine's hunt registry writes "
            "with gitops off and the hunt runner reads. A comma-separated list is refused. "
            "DFE_ENGINE_HUNTS_DIR."
        ),
    )
    rules_dir: str = Field(
        default="",
        description="YAML directory for API-managed detection rules (DirectoryConfigStore SSoT)",
    )
    checkpoint_timestamp_field: str = Field(
        default="_timestamp_load",
        description="Timestamp field for checkpointing (the common header's load column)",
    )
    runner_poll_seconds: float = Field(
        default=15.0,
        gt=0,
        description=(
            "Seconds between dfe-hunt-runner ticks. The API reports it so a caller "
            "knows how long a queued run-now waits before it is claimed."
        ),
    )
    alert_channels: list[str] = Field(
        default_factory=list,
        description="Global Apprise notification URLs applied to all hunts (e.g. slack://token/#channel)",
    )
    alert_destinations: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Named alert destinations: {name: apprise_url}."
            " Inline bootstrap; prefer alert_destinations_dir."
        ),
    )
    alert_destinations_dir: str = Field(
        default="",
        description="YAML directory for alert destination definitions (DirectoryConfigStore SSoT)",
    )
    max_detections_per_run: int = Field(
        default=MAX_DETECTIONS_PER_RUN,
        gt=0,
        description=(
            "Detection rows one rule may write in one hunt run. Past it the run writes "
            "this many plus one summary row carrying the true match count."
        ),
    )
    max_detections_per_run_ceiling: int = Field(
        default=MAX_DETECTIONS_PER_RUN_CEILING,
        gt=0,
        description="Upper bound on max_detections_per_run; a larger value is cut to this.",
    )
    detection_guard: DetectionGuardSettings = Field(
        default_factory=DetectionGuardSettings,
        description="Alert-volume preview on rule create and update",
    )
    lookup_databases: list[str] = Field(
        default_factory=list,
        description=(
            "Shared reference databases a rule may look values up in, as "
            "IN (SELECT <columns> FROM <db>.<table> [WHERE <condition>]) or IN <db>.<table>. "
            "The hunt runner's ClickHouse user reads every org, so a listed database must "
            "hold no org's rows. The hunt runner refuses to start, or stops at its next "
            "reload, while one of its tables has an _org_id column or is not a plain store "
            "of its own rows, as a view is. The engine refuses to start on one when "
            "ClickHouse answers at its start, and starts with the check unrun when it does not. "
            "The hunt runner's role needs SELECT on each. Empty refuses every lookup. "
            "DFE_ENGINE_HUNTS_LOOKUP_DATABASES (comma-separated)."
        ),
    )
    window_overlap_seconds: int = Field(
        default=WINDOW_OVERLAP_SECONDS,
        ge=MIN_WINDOW_OVERLAP_SECONDS,
        description=(
            "Seconds before its watermark each rule hunt re-reads, so a row committed or "
            "replicated up to this late is still matched. A row already detected is skipped, "
            "so the overlap costs reads, never a second detection. "
            "DFE_ENGINE_HUNTS_WINDOW_OVERLAP_SECONDS."
        ),
    )
    safe_horizon: bool = Field(
        default=True,
        description=(
            "Hold a hunt's fire until it is behind the safe horizon: the newest "
            "_timestamp_load every replica already holds. A hunt never reads past it. "
            "Measured as now less the replication delay system.replicas reports for the "
            "hunt's source and detection tables, less a commit allowance. "
            "DFE_ENGINE_HUNTS_SAFE_HORIZON (true/false)."
        ),
    )
    safe_horizon_max_deferral_seconds: int = Field(
        default=SAFE_HORIZON_MAX_DEFERRAL_SECONDS,
        ge=0,
        description=(
            "Seconds a hunt may be held for a lagging replica, counted from the first poll "
            "it was held across every fire since, cut to the hunt's interval. Past it the "
            "fire runs anyway, and the next fire re-reads from the horizon it ran short of. "
            "DFE_ENGINE_HUNTS_SAFE_HORIZON_MAX_DEFERRAL_SECONDS."
        ),
    )

    @field_validator("hunt_dir")
    @classmethod
    def _hunt_dir_is_one_directory(cls, value: str) -> str:
        directory = value.strip()
        if "," in directory:
            raise ValueError(
                f"hunts.hunt_dir {directory!r} must name one directory, which the engine "
                "and the hunt runner both read"
            )
        return directory

    @field_validator("lookup_databases")
    @classmethod
    def _lookup_databases_are_plain_names(cls, value: list[str]) -> list[str]:
        names = [name.strip() for name in value]
        for name in names:
            if re.fullmatch(LOOKUP_DATABASE_PATTERN, name) is None:
                raise ValueError(
                    f"lookup database {name!r} is not a plain database name "
                    "(letters, digits, '_' and '-', not starting with a digit)"
                )
            if name.lower() in _CLICKHOUSE_CATALOGUES:
                raise ValueError(
                    f"lookup database {name!r} is one of ClickHouse's own, which describe "
                    "every org's tables and queries"
                )
        return list(dict.fromkeys(names))


class ArtifactorySettings(BaseModel):
    """Artifactory settings for downloading templates."""

    url: str = Field(default="")
    username: str = Field(default="")
    password: str = Field(default="")
    templates_version: str = Field(default="latest")


class TransportSettings(BaseModel):
    """What this deployment can carry a source's records on, between its stages.

    A source records ``bus`` or ``direct`` and nothing more. Which bus and which
    direct protocol are facts of the deployment, so they live here: a second bus
    provider joins behind the same ``bus`` value without touching a source.

    ONE transport per deployment. Every stage is bound to it at deploy time - on
    the bus the loader consumes the topic, on direct it listens for a push - and
    no stage does both, so a source on the other transport lands nowhere.
    ``default`` names the one this deployment was stood up with; ``bus_present``
    says whether a broker exists here at all, which is what topic creation
    follows.

    The mesh pair is the same kind of fact one level down: on direct a stage is a
    pool of pods, and a deployment either dials the pool's Service or puts a
    balancing listener in front of it. The engine compiles a sender's destination,
    so it has to address the pool the way the charts front it.

    """

    default: SourceTransport = Field(
        default="bus",
        description=(
            "The transport this deployment's stages are bound to, and the one a "
            "source that names none takes"
        ),
    )
    bus_present: bool = Field(
        default=True,
        description="Whether this deployment runs a bus at all; false means direct only",
    )
    bus_provider: str = Field(
        default="kafka",
        description="Which bus carries the ``bus`` transport (reported, never chosen per source)",
    )
    direct_protocol: str = Field(
        default="grpc",
        description="Which protocol carries the ``direct`` transport",
    )
    mesh_enabled: bool = Field(
        default=False,
        description="Whether this deployment's stage pools sit behind balancing listeners",
    )
    mesh_namespace: str = Field(
        default="",
        description="Namespace holding those listeners; the address shape is the app manifest's",
    )

    def available(self) -> set[str]:
        """The transports a source may name here: the one the deployment carries.

        What an app CAN carry is a capability in apps.yaml; this is what this
        deployment DOES carry, and only that reaches a running stage.
        """
        return {self.default}

    @model_validator(mode="after")
    def _a_bus_default_needs_a_bus(self) -> TransportSettings:
        # Every source would take a topic no broker holds, and the reason would sit
        # two config keys away from the source being written.
        if self.default == "bus" and not self.bus_present:
            raise ValueError(
                "transport.default is 'bus' but this deployment runs no bus: set "
                "DFE_ENGINE_TRANSPORT_DEFAULT=direct, or DFE_ENGINE_TRANSPORT_BUS_PRESENT=true"
            )
        return self

    @model_validator(mode="after")
    def _mesh_needs_a_namespace(self) -> TransportSettings:
        # The address is the manifest's shape plus this namespace, so an unset one
        # compiles every sender a destination with a hole where the host goes.
        if self.mesh_enabled and not self.mesh_namespace:
            raise ValueError(
                "transport.mesh_enabled is on but no namespace holds the listeners: set "
                "DFE_ENGINE_MESH_NAMESPACE, or DFE_ENGINE_MESH_ENABLED=false to dial the pools directly"
            )
        return self


class KafkaSettings(BaseModel):
    """Kafka connection settings.

    SASL fields are only needed where the engine itself talks to the brokers -
    today that is the sampler's Kafka consumer (recent-tail + logreducer
    KafkaSource). Per the credential contract (dfe-engine#98) the mechanism is
    DERIVED from ``provider``, never hand-set: set ``provider`` (+ username +
    password) for a real cluster; leave it ``plaintext`` for a local dev broker
    with no auth. See ``dfe_engine.kafka.contract``. An empty provider keeps any
    hand-set protocol/mechanism (transition) but the security floor still applies.
    """

    provider: str = Field(
        default="",
        description=(
            "Kafka provider; DERIVES security_protocol + sasl_mechanism per the "
            "contract (strimzi/redpanda/msk/redpanda-cloud -> SASL_SSL + "
            "SCRAM-SHA-512; strimzi-no-tls/redpanda-no-tls -> SASL_PLAINTEXT + "
            "SCRAM-SHA-512 for the in-cluster TLS-off listener; confluent-cloud -> "
            "PLAIN; plaintext -> no auth; msk_iam -> quarantined IAM path). Never "
            "hand-set the mechanism."
        ),
    )
    bootstrap_servers: str = Field(default="localhost:9092")
    external_bootstrap_servers: str = Field(
        default="",
        description=(
            "The broker address a pipeline the customer runs outside the deployment "
            "connects to (DFE_ENGINE_KAFKA_EXTERNAL_BOOTSTRAP_SERVERS). Shown to the "
            "operator in a source's external connection. Empty shows bootstrap_servers, "
            "which is right where the customer's pipeline runs beside the brokers"
        ),
    )
    security_protocol: str = Field(default="PLAINTEXT")
    sasl_mechanism: str = Field(
        default="",
        description="DERIVED from provider (contract); do not hand-set.",
    )
    sasl_username: str = Field(default="", description="SASL username")
    sasl_password: str = Field(default="", description="SASL password")
    ensure_topics: bool | None = Field(
        default=None,
        description=(
            "Manage a source's _land/_load topics: created when it deploys, deleted "
            "when the source is. Unset follows ``transport.bus_present``, so a "
            "brokerless profile asks for nothing without a second dial being set to "
            "match. True or false is the operator's override on top of that fact. "
            "A delete never fails on the topic step. A deploy of a source on the bus "
            "fails when a topic is not created."
        ),
    )
    bootstrap_topics: bool = Field(
        default=True,
        description=(
            "Create the dfe-schemas bootstrap topic set at startup, create-only "
            "(DFE_ENGINE_KAFKA_BOOTSTRAP_TOPICS). Separate from the ClickHouse dial because "
            "the two fail for different reasons and a deployment with an external "
            "broker wants one off and the other on. A deployment with no bus skips "
            "the set and still converges."
        ),
    )
    tiered_storage: bool = Field(
        default=False,
        description=(
            "Whether this deployment's brokers tier to object storage "
            "(DFE_ENGINE_KAFKA_TIERED_STORAGE). On, the landing topic is created with "
            "``remote.storage.enable``; off, the key is absent rather than set "
            "false, so a broker-level setting is left to decide. Tiering is a "
            "per-topic switch as well as a per-broker one: with the broker "
            "plugin on and the topic key missing, every segment stays on local "
            "disk until the volume fills."
        ),
    )
    topic_partitions: int = Field(
        default=3,
        ge=1,
        le=1000,
        description=(
            "Partition count for topics DFE creates. Capped: a mistyped value asks a "
            "real broker for that many partitions per topic, costing file handles and "
            "controller metadata across the cluster."
        ),
    )
    topic_replication_factor: int | None = Field(
        default=None,
        ge=1,
        le=10,
        description=(
            "Replication factor for each source's _land and _load topics "
            "(DFE_ENGINE_KAFKA_TOPIC_REPLICATION_FACTOR), used as given when set. Unset "
            "takes the dfe-schemas landing default clamped to the brokers the bus "
            "reports, so a managed broker that refuses fewer than 3 replicas gets 3 "
            "and a one-broker dev cluster gets 1."
        ),
    )
    topic_retention_ms: int | None = Field(
        default=None,
        ge=-1,
        description=(
            "``retention.ms`` for topics DFE creates and converges. Unset leaves the "
            "broker default in place, which is what an untouched deployment already "
            "has; -1 is Kafka's infinite retention."
        ),
    )
    topic_cleanup_policy: str = Field(
        default="",
        description=(
            "``cleanup.policy`` for topics DFE creates and converges (``delete`` or "
            "``compact``). Empty leaves the broker default in place."
        ),
    )
    topic_compression_type: str = Field(
        default="producer",
        description=(
            "``compression.type`` for topics DFE creates and converges. ``producer`` "
            "keeps each batch in the codec its producer chose, so the broker never "
            "recompresses it. Empty leaves the broker default in place."
        ),
    )
    topic_max_message_bytes: int | None = Field(
        default=None,
        gt=0,
        description=(
            "``max.message.bytes`` for every topic DFE creates and converges: the "
            "bootstrap set and each source's _land and _load "
            "(DFE_ENGINE_KAFKA_TOPIC_MAX_MESSAGE_BYTES). Set it to the deployment's message "
            "size, the one its broker and producers carry. Unset falls back to the "
            "dfe-schemas manifest's size, which a managed broker capped below it "
            "refuses at create."
        ),
    )

    @model_validator(mode="after")
    def _derive_from_provider(self) -> KafkaSettings:
        """Derive protocol + mechanism from provider, then enforce the contract."""
        from dfe_engine.kafka.contract import derive, validate

        if self.provider:
            self.security_protocol, self.sasl_mechanism = derive(self.provider)
        validate(
            security_protocol=self.security_protocol,
            sasl_mechanism=self.sasl_mechanism,
        )
        return self


class StorageSettings(BaseModel):
    """Storage backend settings for artifacts and templates.

    Supports local filesystem, S3, and HTTP backends.
    The 'type' field is auto-detected from 'path' if not explicitly set:
    - Paths starting with 's3://' -> S3 backend
    - Paths starting with 'http://' or 'https://' -> HTTP backend
    - All other paths -> Local filesystem backend
    """

    type: str = Field(default="auto", description="Storage type: auto, local, s3, http")
    path: str = Field(default="./artifacts", description="Storage path/URL")
    s3_region: str = Field(default="", description="AWS region (for s3 type)")


class QueryViewSettings(BaseModel):
    """Settings for ClickHouse parameterized view execution.

    Controls the restricted RBAC user, resource limits, and catalog caching.

    """

    restricted_user: str = Field(
        # The query_reader service user minted by governance.ch.ChRbacReconciler.
        default="dfe_query_reader",
        description="Username for the restricted query user (the query_reader service user)",
    )
    restricted_password: str = Field(
        default="",
        description=(
            "Password for the restricted query user. Empty reads the minted service "
            "user's own password: the deployment-provided one, else the secrets store "
            "(ch/service/query_reader for dfe_query_reader)."
        ),
    )
    view_prefix: str = Field(default="dfe_v_", description="Prefix for parameterized view names")
    catalog_cache_ttl: int = Field(default=60, description="View catalog cache TTL in seconds")
    default_limit: int = Field(default=1000, description="Default row limit")
    max_limit: int = Field(default=100_000, description="Maximum allowed row limit")
    default_timeout: int = Field(default=30, description="Default query timeout in seconds")
    max_timeout: int = Field(default=300, description="Maximum allowed timeout in seconds")
    max_execution_time: int = Field(
        default=30,
        description="ClickHouse settings profile max_execution_time (seconds)",
    )
    max_rows_to_read: int = Field(
        default=10_000_000,
        description="ClickHouse settings profile max_rows_to_read",
    )
    max_memory_usage: int = Field(
        default=2_147_483_648,
        ge=1,
        description="ClickHouse settings profile max_memory_usage, in bytes",
    )


class SamplerSettings(BaseModel):
    """Source-sampling settings (the /sources/{source}/sample API + `dfe sample`).

    Two families of mode: cheap "recent"/"random" reads that run inline, and the
    memory-hungry logreducer modes ("smart"/"anomaly") that are gated. logreducer
    is memory-hungry, so its concurrency is capped at ``max_concurrent`` instances,
    each bounded to ``max_memory_gb`` - both small by default.

    """

    default_mode: Literal["recent", "random", "smart", "anomaly"] = Field(
        default="smart",
        description="Mode of a sample request that names none: recent | random | smart | anomaly",
    )
    default_limit: int = Field(default=100, ge=1, description="Default rows returned")
    max_limit: int = Field(default=10_000, ge=1, description="Hard cap on rows returned")
    level: str = Field(
        default="enhanced",
        description="logreducer level for smart/anomaly: standard | enhanced | maximum",
    )
    max_concurrent: int = Field(
        default=2, ge=1, description="Max concurrent logreducer runs (N instances)"
    )
    max_memory_gb: float = Field(
        default=1.0, gt=0, description="Memory ceiling per logreducer run (X GB)"
    )
    max_scan_rows: int = Field(
        default=50_000,
        ge=1,
        description="Rows fed to a logreducer run (bounds the CH scan / Kafka read)",
    )
    kafka_max_messages: int = Field(
        default=20_000, ge=1, description="Max Kafka messages read per sample pass"
    )
    timestamp_field: str = Field(
        default="_timestamp_load",
        description="Column used to order 'recent' samples (DESC), the common header's load column",
    )
    wait_seconds: float = Field(
        default=8.0,
        ge=0,
        description="Seconds the submit endpoint blocks for inline completion (fast modes)",
    )
    max_execution_time: int = Field(
        default=30, ge=1, description="ClickHouse max_execution_time (s) for sample reads"
    )


class SyntheticDataSettings(BaseModel):
    """Synthetic reference-data generation (the /synthetic-data API + demo streams).

    Synthetic data is a demo/test stream, never a load generator: every knob here is a
    ceiling that keeps a fat-fingered request modest. Rates are Poisson-paced
    events per second; streams are bounded by count and/or duration.

    A standing demo stream (the sales-demo live tail) is the AUTOSTART knobs:
    name reference packs and a receiver URL and the engine keeps those streams
    running for the pod's lifetime. Default empty = off.

    """

    max_count: int = Field(
        default=10_000, ge=1, description="Hard cap on events per inline generate call"
    )
    max_rate_eps: float = Field(
        default=200.0, gt=0, description="Hard cap on stream rate (events/second)"
    )
    max_stream_seconds: float = Field(
        default=28_800.0, gt=0, description="Hard cap on a stream task's duration (default 8h)"
    )
    default_rate_eps: float = Field(
        default=5.0, gt=0, description="Stream rate when the caller does not set one"
    )
    autostart_schemas: str = Field(
        default="",
        description="Comma-separated pack refs to stream continuously (empty = off)",
    )
    autostart_receiver_url: str = Field(
        default="", description="Ingest URL the standing streams post to"
    )
    autostart_rate_eps: float = Field(
        default=2.0, gt=0, description="Per-pack rate for the standing streams"
    )
    autostart_seed: int | None = Field(
        default=None, description="Determinism seed for the standing streams"
    )
    max_concurrent_streams: int = Field(
        default=3, ge=1, description="Hard cap on concurrently running API stream tasks"
    )
    allowed_receiver_hosts: str = Field(
        default="",
        description=(
            "Comma-separated hostnames stream receiver URLs may target (empty = any http(s))"
        ),
    )


class KedaShimSettings(BaseModel):
    """dfe-keda-shim settings - the KEDA metrics-api -> ClickHouse query adapter.

    The shim answers KEDA's ``metrics-api`` polls by running a config-defined
    ClickHouse query and returning ONE integer, with a last-good fail-safe cache: on
    any metric failure it returns the cached value so KEDA sees no change and scaling
    FREEZES at current (a broken metric can never run replicas up and blow the bill).
    Queries live in a YAML catalogue (built-in ``keda_shim/queries.yaml`` + an
    optional mounted override), so a HyperDX schema rename or a new scaling signal is
    a CONFIG edit, never a shim rebuild.

    """

    host: str = Field(default="0.0.0.0", description="Shim HTTP bind address")  # noqa: S104
    port: int = Field(default=8080, ge=1, description="Shim HTTP port KEDA polls")
    query_config: str = Field(
        default="",
        description=(
            "Path to a YAML query catalogue that OVERRIDES/extends the built-in "
            "defaults (deep-merged). Empty = built-in pressure + backlog only."
        ),
    )


class RepositorySettings(BaseModel):
    """Repository (scope-aligned small-object store) settings.

    The repository lives in ``clickhouse.data_database`` with every other DFE
    table -- it carries no database of its own.

    """

    max_prefs_bytes: int = Field(
        default=262144,
        ge=1,
        description="Max serialized size (bytes) of a preferences document",
    )
    max_object_bytes: int = Field(
        default=1048576,
        ge=1,
        description="Max size (bytes) of a stored object value",
    )


class BackingFactSettings(BaseModel):
    """What the deployer states about one backing service, and nothing it detects.

    Empty everywhere means the deployer stated nothing, which every deployment that
    runs its own Kafka and ClickHouse is, so the engine reports the service as one
    DFE runs.
    """

    provider: str = Field(
        default="",
        description=(
            "The provider key the deployment dial chose for this service "
            "(strimzi, msk, confluent-cloud, clickhouse-cloud, external, ...). "
            "The provider catalogue says what the key means; a key it does not "
            "list reads as DFE-run unless the deploy repo declares mode external."
        ),
    )
    tier: str = Field(
        default="",
        description="The provider's tier or instance type as the dial set it; reported only.",
    )
    region: str = Field(
        default="",
        description="The provider region the service runs in; reported only.",
    )

    @field_validator("provider")
    @classmethod
    def _provider_key(cls, value: str) -> str:
        # Catalogue keys are lower-case identifiers, so a deployer's casing cannot miss one.
        return value.strip().lower()


class BackingSettings(BaseModel):
    """The deployer's facts for each backing service the engine reports on."""

    kafka: BackingFactSettings = Field(default_factory=BackingFactSettings)
    clickhouse: BackingFactSettings = Field(default_factory=BackingFactSettings)

    def facts(self, service: str) -> BackingFactSettings:
        """The facts for *service*, empty for a service the deployer cannot state facts about."""
        stated = {"kafka": self.kafka, "clickhouse": self.clickhouse}
        return stated.get(service, BackingFactSettings())


class DeploymentSettings(BaseModel):
    """Deployment configuration settings."""

    backing: BackingSettings = Field(
        default_factory=BackingSettings,
        description=(
            "Who runs each backing service, as the deployer states it. Injected by "
            "the deployer, never detected, and kept apart from kafka.provider, "
            "which derives the engine's own broker protocol."
        ),
    )

    profile: str = Field(
        default="",
        description=(
            "The tier this deployment was stood up as (slim, single, scale, "
            "mesh, or a deployer's own name). Injected by the deployer from "
            "the one place it is already known, never detected, and reported "
            "rather than interpreted: the engine judges an app's offer against it "
            "but never changes what it deploys. Empty means unknown, which a "
            "hand-run engine is; every deployer names one, dfe-docker included. "
            "DFE_ENGINE_PROFILE."
        ),
    )
    admin_links: str | list[Any] = Field(
        default_factory=list,
        description=(
            "The admin UIs this deployment runs, as a JSON array of objects with "
            "name, purpose, url and an optional probe_url. Injected by the deployer, "
            "which knows what it stood up and where a browser reaches each one, "
            "never discovered. url is what a browser opens; probe_url is the "
            "in-network address the engine GETs to report the UI up or down. Held "
            "as given and read at startup, where an entry that does not validate is "
            "dropped and counted rather than stopping the engine. Empty means none "
            "listed. DFE_ENGINE_ADMIN_LINKS."
        ),
    )
    namespace: str = Field(
        default="",
        description=(
            "Kubernetes namespace this engine runs in, injected by the chart from "
            "metadata.namespace. Names the namespace in the credential-fetch command "
            "the login page shows. DFE_ENGINE_DEPLOYMENT_NAMESPACE."
        ),
    )
    target: Literal["kubernetes", "docker", "unknown"] = Field(
        default="unknown",
        description=(
            "Where this deployment runs. Gates the scaling dials: a Compose "
            "deployment has no KEDA and sets CPU and memory stack-wide, so the "
            "dials are reported unsupported rather than silently accepted. "
            "Defaults to unknown so an unset deployment refuses rather than "
            "guesses. Injected by the deployer, not detected."
        ),
    )
    app_config_dir: str = Field(
        default="",
        description=(
            "Directory the engine renders each app's own config file into, one "
            "sub-directory per app. Set by a deployer that has no chart to do it "
            "and mounts the result into its containers; empty means something "
            "else delivers the overlay, which on Kubernetes is the app's chart. "
            "DFE_ENGINE_DEPLOYMENT_APP_CONFIG_DIR."
        ),
    )
    app_config_base_dir: str = Field(
        default="",
        description=(
            "Directory holding the deployment's own base config per app, as "
            "<service>.yaml. The instance overlay is merged OVER it, so brokers, "
            "warehouse credentials and spool paths stay with the deployment "
            "instead of being committed to the deploy repo. "
            "DFE_ENGINE_DEPLOYMENT_APP_CONFIG_BASE_DIR."
        ),
    )
    app_config_mount: str = Field(
        default="",
        description=(
            "Path the APPS see app_config_dir at, where the two differ - the "
            "engine and the app mount one directory at their own paths, and a "
            "rendered file names the app's. Empty means they are the same. "
            "DFE_ENGINE_DEPLOYMENT_APP_CONFIG_MOUNT."
        ),
    )
    app_env_dir: str = Field(
        default="",
        description=(
            "Directory the engine writes each app's custom environment into, as "
            "<service>.custom.env, for a deployer whose containers read env files "
            "from the host. Each file is written 0640 with the directory's group, "
            "so the engine must hold that group. Empty means something else "
            "delivers the overlay's extraEnv block, which on Kubernetes is the "
            "app's own chart. DFE_ENGINE_DEPLOYMENT_APP_ENV_DIR."
        ),
    )


class TransformValidationSettings(BaseModel):
    """Syntax validation for authored transform files."""

    enabled: bool = Field(
        default=False,
        description=(
            "Check authored VRL and Vector transforms before committing them. Off by "
            "default: the VRL backend links against compiled Vector artifacts a "
            "deployment need not have, and an absent backend must not look like a "
            "passing check."
        ),
    )
    blocking: bool = Field(
        default=True,
        description=(
            "Refuse a write whose content fails validation. Only an explicit failure "
            "blocks; an unavailable backend never does."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "Allow running an authored transform over sampled events. Off by default: "
            "it executes caller-supplied code, so a deployment opts in rather than "
            "inherits it from the validation switch."
        ),
    )
    allow_host_functions: bool = Field(
        default=False,
        description=(
            "Allow a VRL transform to call get_env_var, get_secret, http_request, "
            "dns_lookup and reverse_dns. Off by default, so a save that calls one is "
            "refused: they read the pod environment, which holds the Kafka credential, "
            "and reach other hosts. Applies whether or not validation is enabled."
        ),
    )


class SchemasSettings(BaseModel):
    """Shared schemas settings (dfe-schemas tree)."""

    schemas_dir: str = Field(
        default="",
        description="Root of dfe-schemas directory (overrides the installed package)",
    )
    e2e_schema_fixtures_dir: str = Field(
        default="",
        description=(
            "Where the e2e schema seeds write. Outside the schema tree, so a seed "
            "cannot add files to the packaged schemas or a git worktree. Unset "
            "means those seeds refuse. DFE_ENGINE_E2E_SCHEMA_FIXTURES_DIR."
        ),
    )


class SourceSettings(BaseModel):
    """Source registry settings."""

    sources_dir: str = Field(default="", description="YAML directory for Source definitions (SSoT)")
    builds_dir: str = Field(
        default="",
        description="Persisted schema build artifacts (source-builds)",
    )
    plans_dir: str = Field(
        default="",
        description="ClickHouse deploy dry-run plans (source-plans)",
    )
    deploys_dir: str = Field(
        default="",
        description="ClickHouse deploy run results (source-deploys)",
    )
    allow_mixed_fetcher_types: bool = Field(
        default=False,
        description=(
            "Let one source, and the fetcher composed for it, carry more than one "
            "connector type. Unsupported: the records no longer share a schema"
        ),
    )


class FieldMapSettings(BaseModel):
    """Field map registry settings."""

    fieldmaps_dir: str = Field(
        default="", description="YAML directory for FieldMap definitions (SSoT)"
    )


class SigmaSettings(BaseModel):
    """Sigma detection pipeline settings."""

    enabled: bool = Field(
        default=False,
        description="Mount the /api/v1/sigma API; while off every path under it answers 404",
    )


class ServicesSettings(BaseModel):
    """Where the engine reaches the managed DFE services.

    The transform-wasm compile/test proxy and the Rust-services YAML config
    replica dir.

    """

    transform_wasm_url: str = Field(default="http://localhost:8080")
    transform_wasm_compiler_url: str = Field(default="http://localhost:8090")
    config_yaml_dir: str = Field(
        default="", description="YAML config replica directory for Rust services"
    )


class SeedAccount(BaseModel):
    """One named local account seeded + RECONCILED on every bootstrap.

    Unlike the local admin, whose owner's own password survives a restart, each
    seed account is reconciled on every startup: the configured password and groups
    WIN, so a full teardown+rebuild restores the exact shared team logins
    unchanged (dfe-infra #106). A runtime password change to a seed account is
    therefore reasserted to the configured value on the next rebuild -- these are
    shared, config-owned logins, not self-service personal credentials.
    """

    username: str = Field(description="Login username")
    password: str = Field(
        default="",
        description="Password, reconciled to win on every boot. Empty -> account left unusable.",
    )
    groups: list[str] = Field(
        default_factory=list,
        description="Group memberships to reconcile (e.g. dfe-analysts, dfe-viewers).",
    )
    email: str = Field(
        default="",
        description="Contact email. Empty -> left unchanged on reconcile.",
    )


class LocalAuthSettings(BaseModel):
    """Built-in local-account bootstrap config (nested under auth.local).

    When enabled, the engine seeds the admin account from ``admin_password`` and
    reconciles ``seed_accounts`` on every bootstrap.
    """

    enabled: bool = Field(default=False, description="Seed built-in local accounts")
    admin_name: str = Field(default="", description="Bootstrap admin username")
    admin_password: str = Field(default="", description="Bootstrap admin password")
    seed_accounts: list[SeedAccount] = Field(
        default_factory=list,
        description=(
            "Named accounts reconciled on EVERY startup (config wins), so shared team "
            "logins survive a teardown+rebuild unchanged (dfe-infra #106). Set from "
            "DFE_ENGINE_AUTH_LOCAL_SEED_ACCOUNTS: a JSON list of "
            "{username, password, groups}. Profile-independent (same across slim/single/scale)."
        ),
    )
    breakglass_password: str = Field(
        default="",
        description=(
            "First-boot password for the break-glass recovery admin. Hashed into the "
            "deploy repo's governance settings when no hash is committed yet, and "
            "ignored from then on. DFE_ENGINE_AUTH_BREAKGLASS_PASSWORD."
        ),
    )
    recovery_email: str = Field(
        default="",
        description=(
            "Contact email for the bootstrap admin and break-glass recovery accounts. "
            "DFE_ENGINE_RECOVERY_EMAIL. Empty falls back to {username}@dfe.local."
        ),
    )
    admin_password_secret_path: str = Field(
        default="",
        description=(
            "Where in the secret store the deployment keeps the admin password. Set "
            "= the rotation endpoint writes the new password there; empty = the "
            "password is changed where the deployment keeps it, and the endpoint "
            "returns 501 with the command. DFE_ENGINE_AUTH_LOCAL_ADMIN_PASSWORD_SECRET_PATH."
        ),
    )
    admin_secret_name: str = Field(
        default="dfe-engine",
        description=(
            "Secret holding the admin password, for the credential-fetch command the "
            "login page shows while setup is incomplete. DFE_ENGINE_AUTH_LOCAL_ADMIN_SECRET_NAME."
        ),
    )
    admin_secret_key: str = Field(
        default="admin-password",
        description=("Key within admin_secret_name. DFE_ENGINE_AUTH_LOCAL_ADMIN_SECRET_KEY."),
    )


class LoginThrottleSettings(BaseModel):
    """Backoff after failed sign-ins, per username and per client address.

    Counted in each engine process, so N replicas allow up to N times the attempts.
    Past the threshold each further failure doubles the wait, from two seconds up to
    ``max_delay_seconds``; a key forgets its failures after the same span with none.

    """

    enabled: bool = Field(default=True, description="Throttle failed sign-ins")
    username_failures: int = Field(
        default=5,
        ge=1,
        description=(
            "Failed sign-ins for one username before it must wait. Counted whatever "
            "address they come from, so an attacker can hold one account's sign-in "
            "back for up to max_delay_seconds at a time."
        ),
    )
    client_failures: int = Field(
        default=20,
        ge=1,
        description=(
            "Failed sign-ins from one client address before it must wait. The address "
            "is the proxy's unless api.forwarded_allow_ips trusts it, and then every "
            "caller shares one count."
        ),
    )
    max_delay_seconds: int = Field(
        default=900, ge=1, description="Longest wait one failure can impose, in seconds"
    )


class AuthSettings(BaseModel):
    """Authorization settings.

    Bespoke role->permission RBAC. Zero external dependencies.

    """

    enabled: bool = Field(
        default=True,
        description=(
            "Enable authorization. Defaults ON so it matches env's default "
            "'production' posture -- with it off, api/deps.py hands an "
            "unauthenticated request the admin role. Dev and test opt out with "
            "DFE_ENGINE_AUTH_ENABLED=false, which needs DFE_ENGINE_ENV set to a dev posture too "
            "(see DFESettings._reject_insecure_production_posture)."
        ),
    )
    auth_dir: str = Field(
        default="",
        description="Auth config directory (accounts, groups, api-keys)",
    )
    trust_proxy_auth_headers: bool = Field(
        default=False,
        description=(
            "Trust the proxy-set X-Oidc-* identity headers (auth Path 1). Enable "
            "ONLY when a trusted proxy (e.g. Envoy Gateway) authenticates the user "
            "and injects these headers AND the engine is reachable only via that "
            "proxy. Default off = fail closed: standalone/unfronted deployments "
            "ignore these client-spoofable headers. The audited client address is "
            "decided by api.forwarded_allow_ips, not by this."
        ),
    )
    oidc_group_sync_enabled: bool = Field(
        default=True,
        description=(
            "Sync each enabled api-mode OIDC provider's groups every sync_interval in the background. "
            "Off leaves only POST /oidc-providers/{name}/sync. DFE_ENGINE_AUTH_OIDC_GROUP_SYNC_ENABLED."
        ),
    )
    oidc_group_sync_tick_seconds: int = Field(
        default=60,
        ge=1,
        description=(
            "How often the background group sync checks which providers are due. "
            "DFE_ENGINE_AUTH_OIDC_GROUP_SYNC_TICK_SECONDS."
        ),
    )
    oidc_login_directory_deadline_seconds: float = Field(
        default=10.0,
        gt=0,
        description=(
            "Longest one OIDC login waits for the directory's group lookup, every page "
            "and retry included. Past it the login keeps the groups it already has, "
            "the same as a directory that does not answer. Each page alone may take "
            "the HTTP client's full timeout, so without this a slow directory can hold "
            "a login for minutes. DFE_ENGINE_AUTH_OIDC_LOGIN_DIRECTORY_DEADLINE_SECONDS."
        ),
    )
    proxy_provider: str = Field(
        default="oidc",
        description=(
            "OIDC provider name the trusted front proxy authenticates against, "
            "stamped on an account the proxy path provisions (auth Path 1). A "
            "deployment running BOTH the proxy path and the engine-as-relying-party "
            "path sets this to the same provider name the RP callback uses, so one "
            "IdP's accounts are not two identities. Default 'oidc' is the protocol, "
            "not a provider: it keeps a proxy-only deployment's existing accounts "
            "reachable, and it matches no RP provider name. "
            "DFE_ENGINE_AUTH_PROXY_PROVIDER."
        ),
    )
    source_provider_bindings: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Maps a stored account's source_provider stamp to the ONE OIDC provider "
            "name allowed to adopt it at login. The SCIM case is why it exists: a "
            "user the SCIM face provisions is stamped 'scim', and without a binding "
            "their first OIDC login is refused as a cross-identity write. "
            "{'scim': 'entra'} declares that the SCIM connector and the 'entra' OIDC "
            "provider are one identity source. Empty (the default) binds nothing and "
            "every such account stays refused -- fail closed. A stamp maps to one "
            "provider, so a second IdP can never claim the same accounts, and an "
            "account with NO stamp is a local credential that no binding can name. "
            "DFE_ENGINE_AUTH_SOURCE_PROVIDER_BINDINGS, a JSON object."
        ),
    )
    local: LocalAuthSettings = Field(default_factory=LocalAuthSettings)
    login_throttle: LoginThrottleSettings = Field(default_factory=LoginThrottleSettings)
    api_key_default_ttl_days: int = Field(
        default=90,
        ge=0,
        description=(
            "Lifetime of an API key created without expires_at, in days. 0 creates "
            "such a key with no expiry. A key that names its own expires_at keeps it. "
            "DFE_ENGINE_AUTH_API_KEY_DEFAULT_TTL_DAYS."
        ),
    )

    @field_validator("source_provider_bindings")
    @classmethod
    def _refuse_binding_chains(cls, bindings: dict[str, str]) -> dict[str, str]:
        # A binding joins a stamp to one provider, so a target that is itself a stamp joins names no one bound.
        chained = sorted(stamp for stamp, target in bindings.items() if target in bindings)
        if chained:
            links = ", ".join(f"{stamp} -> {bindings[stamp]}" for stamp in chained)
            raise ValueError(
                f"source_provider_bindings may not chain: {links} binds to a name that is "
                "itself bound; bind each stamp straight to its OIDC provider"
            )
        return bindings


class HyperDXSettings(BaseModel):
    """HyperDX integration settings."""

    base_url: str = Field(default="", description="HyperDX API base URL")
    enabled: bool = Field(default=False, description="Enable HyperDX integration")


class GitopsWriteSettings(BaseModel):
    """Timeout and retry budget for the remote calls a deploy-repo write makes.

    A write fetches the remote head before it commits and pushes after. Each call
    waits up to ``timeout_seconds`` on any one connect or read, longer than reads
    wait, because a push to a small forge under load or through its pre-receive
    hooks can take that long.

    A transient failure (timeout, refused or reset connection, 408/429/5xx) is
    retried with jittered exponential back-off for ``budget_seconds`` after the
    first failure, and a retry's timeout is capped at the budget left. A forge that
    stops answering fails a call in about ``timeout_seconds + budget_seconds``, and
    the API answers 503. The bound is per read, so a forge that keeps answering
    slowly is not cut off, and a write that must re-apply makes two fetches and two
    pushes. Field names mirror scalo's :class:`~scalo.resilience.ResilienceConfig`;
    a budget of 0 makes one attempt.

    After ``failure_threshold`` calls in a row run out of budget, a circuit breaker
    opens: writes answer 503 at once without calling the forge, with ``Retry-After``
    set to what is left of ``reset_timeout``. Then one write probes the forge, and
    the breaker closes if the forge answers it or reopens if it runs out of budget
    too. A refusal, bad credentials or a missing repo is the forge answering, so it
    never opens the breaker. These two mirror scalo's
    :class:`~scalo.resilience.CircuitBreakerConfig`.

    """

    timeout_seconds: float = Field(
        default=15.0,
        gt=0.0,
        description="Connect and per-read timeout for a write's fetch and push, seconds.",
    )
    wait_initial: float = Field(default=0.5, gt=0.0, description="First back-off, seconds.")
    wait_max: float = Field(default=4.0, gt=0.0, description="Per-attempt back-off cap, seconds.")
    wait_multiplier: float = Field(default=2.0, ge=1.0, description="Exponential back-off factor.")
    budget_seconds: float = Field(
        default=10.0,
        ge=0.0,
        description="How long a call keeps retrying after its first transient failure, seconds.",
    )
    # Three spent budgets is at least 30 s of outage at the defaults, where one forge
    # restart can spend a single budget on its own.
    failure_threshold: int = Field(
        default=3,
        ge=1,
        description="Calls in a row that run out of budget before writes fail at once.",
    )
    # scalo's default: a forge still down costs one probe's budget per window, not one
    # per write, and a recovered one is back in use within the window.
    reset_timeout: float = Field(
        default=30.0,
        gt=0.0,
        description="How long writes fail at once before one probes the forge again, seconds.",
    )


class GitopsReadSettings(BaseModel):
    """How long a deploy-repo read waits on the forge before it answers from the clone.

    Every API read pays a ref advertisement and may pay a fetch behind it, both under
    the tree lock, so a forge that drops packets rather than refusing them must not
    hold a request, or every reader behind it, for the OS connect timeout. A read
    waits up to ``timeout_seconds`` on any one connect or read and is not retried.
    The startup org seed's review-branch check takes the same bound, so a down forge
    cannot hold startup.

    """

    # The cascade's last rung: a config file or DFE_ENGINE_GITOPS_READ_TIMEOUT_SECONDS overrides it.
    timeout_seconds: float = Field(
        default=3.0,
        gt=0.0,
        description="Connect and per-read timeout for a deploy-repo read, seconds.",
    )


class GitopsPackSettings(BaseModel):
    """When the deploy-repo clone packs its objects, and how often it checks.

    dulwich's own gc is switched off in the clone, because it rewrites every object
    the repo holds and would do so inside a write, a read's refresh or a push. The
    engine packs instead. A commit packs the loose objects once ``loose_objects`` of
    them have built up, which costs the same at any history length. A background task
    checks every ``interval_seconds`` and consolidates the packs into one once there
    are ``packs`` of them. A fetch that brings objects adds a pack, and so does each
    loose-object pack. 0 switches either off.

    Consolidation prunes unreachable objects best-effort only. dulwich ages an object
    by the mtime of the file holding it, and every repack rewrites what it keeps, so
    each repack starts an unreachable object's two-week grace period again.

    """

    # git's own gc.auto default.
    loose_objects: int = Field(
        default=6700,
        ge=0,
        description="Loose objects that make a commit pack them first. 0 leaves them loose.",
    )
    # git's own gc.autoPackLimit default.
    packs: int = Field(
        default=50,
        ge=0,
        description="Packs that make the background check consolidate them. 0 never does.",
    )
    interval_seconds: float = Field(
        default=300.0,
        gt=0.0,
        description="How often the background check counts the clone's packs, seconds.",
    )


class GitopsSettings(BaseModel):
    """Deploy-specific gitops repo the engine renders artifacts into.

    Disabled by default. When enabled, the engine clones/pulls repo_url (or uses
    an existing local_path), writes rendered artifacts, commits only on change,
    and pushes when push=True. Secrets never go here -- only declarative config
    Argo consumes.
    """

    enabled: bool = Field(default=False, description="Enable gitops publishing")
    repo_url: str = Field(default="", description="Deploy repo URL (empty = local-only)")
    branch: str = Field(default="main", description="Branch to commit/push")
    local_path: str = Field(default="", description="Working clone path")
    push: bool = Field(default=True, description="Push after commit")
    username: str = Field(default="", description="HTTPS push username")
    token: str = Field(default="", description="HTTPS push token/password")
    author_name: str = Field(default="dfe-engine", description="Commit author name")
    author_email: str = Field(default="dfe-engine@hyperi.io", description="Commit author email")
    mode: Literal["solo", "team"] = Field(
        default="team",
        description=(
            "Operator posture. 'solo' declares a single-operator deployment and "
            "is the explicit override that permits gitops auto-merge in a "
            "production DFE_ENGINE_ENV; 'team' (default) refuses auto-merge outside "
            "dev postures."
        ),
    )
    forge_provider: str = Field(
        default="",
        description=(
            "Git forge that hosts the deploy repo, for opening review PRs when a "
            "production+team write may not commit straight to main: "
            "forgejo|gitea|github|gitlab. Empty = infer from repo_url host."
        ),
    )
    forge_api_base: str = Field(
        default="",
        description=(
            "Override for the forge REST API base URL (e.g. a self-hosted "
            "Forgejo/GitHub Enterprise). Empty = derive from repo_url."
        ),
    )
    read: GitopsReadSettings = Field(default_factory=GitopsReadSettings)
    write: GitopsWriteSettings = Field(default_factory=GitopsWriteSettings)
    pack: GitopsPackSettings = Field(default_factory=GitopsPackSettings)


# Known placeholder JWT secret - fine for local dev, REJECTED in a production
# posture when auth is on (see DFESettings._reject_insecure_production_posture).
_DEV_JWT_SECRET = "dev-secret-key-change-in-production"  # noqa: S105, RUF100 - dev placeholder

# Postures that are NOT production; anything else (incl. the default
# "production") is treated as production for the placeholder-secret guard.
_NON_PROD_ENVS = frozenset({"dev", "development", "local", "test", "ci"})

# The local console dev servers, trusted only by a dev posture that names no origins itself.
_DEV_CORS_ORIGINS = ("http://localhost:5173", "http://localhost:5174", "http://localhost:3000")


def is_dev_posture(env: str) -> bool:
    """True when DFE_ENGINE_ENV declares a non-production posture (dev/local/test/ci)."""
    return env.strip().lower() in _NON_PROD_ENVS


def e2e_routes_enabled(settings: DFESettings) -> bool:
    """True when the unauthenticated /api/e2e/* group may be mounted.

    Requires both the explicit DFE_ENGINE_E2E_SERVER flag and a non-production posture.
    ``make e2e-server`` sets the flag; a production DFE_ENGINE_ENV refuses it at load.
    """
    return bool(settings.e2e_server) and is_dev_posture(settings.env)


class APISettings(BaseModel):
    """API server settings."""

    host: str = Field(default="0.0.0.0", description="API server bind address")  # noqa: S104
    port: int = Field(default=8000, description="API server port")
    cors_origins: list[str] = Field(
        default_factory=list,
        description=(
            "CORS allowed origins, which are also the only origins an OIDC login hands "
            "its token back to (return_to). Empty by default. Left unset in a dev "
            "posture, the local console dev servers' origins are allowed. "
            "DFE_ENGINE_API_CORS_ORIGINS, comma-separated."
        ),
    )
    forwarded_allow_ips: str = Field(
        default="127.0.0.1",
        description=(
            "Comma-separated peer addresses or CIDR networks whose X-Forwarded-Proto "
            "and X-Forwarded-For headers are believed, or '*' to believe any peer. "
            "The scheme the engine builds OIDC redirect URIs from comes from this: "
            "behind a TLS-terminating gateway the peer is the gateway pod, so unless "
            "its address is listed here the request reads as http and the IdP is sent "
            "an http callback URI. So does the client address the audit trail records: "
            "behind a listed peer it is the right-most X-Forwarded-For entry that is "
            "not itself listed, so list every proxy hop, the console pods included. "
            "'*' trusts every hop and records the left-most entry, which the caller "
            "wrote. An entry that is not an address or network stops startup. Default "
            "is loopback only, which fails closed for an engine nothing fronts."
        ),
    )
    jwt_secret: str = Field(
        default=_DEV_JWT_SECRET,
        description=(
            "Keys the OIDC login session cookie when api.session_secret is empty. It "
            "signs no JWT: the JWT authority signs ES384 with the asymmetric key at "
            "api.jwt_key_path in scalo.secrets."
        ),
    )
    jwt_algorithm: str = Field(
        default="ES384",
        description="JWT signing algorithm - ES384 (ECDSA P-384 + SHA-384, CNSA-aligned). Crypto-agile.",
    )
    jwt_expire_minutes: int = Field(default=60, description="JWT token expiry in minutes")
    max_session_minutes: int = Field(
        default=720,
        ge=1,
        description=(
            "Longest a session runs from its sign-in, in minutes. POST /auth/refresh "
            "is refused past it and never mints a token that outlives it, so the "
            "owner signs in again. DFE_ENGINE_API_MAX_SESSION_MINUTES."
        ),
    )
    jwt_issuer: str = Field(
        default="https://dfe.local/api",
        description="JWT issuer (iss) claim + JWKS issuer; set to the deployment engine origin.",
    )
    jwt_key_path: str = Field(
        default="jwt/signing-key",
        description="scalo.secrets path holding the ES384 signing private key (PEM).",
    )
    session_secret: str = Field(
        default="",
        description=(
            "Secret keying the signed session cookie SessionMiddleware uses for the "
            "OIDC RP flow (Authlib state/nonce). Empty -> falls back to jwt_secret."
        ),
    )
    docs_enabled: bool | None = Field(
        default=None,
        description=(
            "Serve Swagger UI at /docs and ReDoc at /redoc. Both pages load their "
            "scripts from a public CDN into the engine's own origin, so unset serves "
            "them in a dev posture only. /openapi.json is served either way. "
            "DFE_ENGINE_API_DOCS_ENABLED."
        ),
    )
    max_active_tasks: int = Field(
        default=64,
        ge=1,
        description=(
            "Background tasks (samples, synthetic streams) one engine process holds "
            "pending or running at once. A submission past it is refused with 429. "
            "DFE_ENGINE_API_MAX_ACTIVE_TASKS."
        ),
    )
    max_request_body_bytes: int = Field(
        default=4 * 1024 * 1024,
        ge=1,
        description=(
            "Largest request body the API accepts, in bytes, on any POST, PUT, PATCH or "
            "DELETE. A body is refused with 413 the moment it passes this, so no config "
            "document over this size is parsed or masked. The routes that take a file "
            "upload are bounded by their own setting instead. "
            "DFE_ENGINE_API_MAX_REQUEST_BODY_BYTES."
        ),
    )
    elastic_converter_max_upload_bytes: int = Field(
        default=5 * 1024 * 1024,
        ge=1,
        description="Max upload size (bytes) for POST /schemas/elastic-converter JSON template",
    )
    elastic_converter_read_chunk_size: int = Field(
        default=64 * 1024,
        ge=1024,
        description="Chunk size when buffering elastic-converter multipart file reads",
    )
    elastic_converter_content_length_slack_bytes: int = Field(
        default=256 * 1024,
        ge=0,
        description=(
            "Multipart bodies exceed raw file size by boundary overhead; allow this many "
            "extra bytes when comparing Content-Length to elastic_converter_max_upload_bytes"
        ),
    )

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_min_length(cls, v: str) -> str:
        # HS256 best practice: signing key >= 32 bytes (RFC 7518 3.2). Fail fast at
        # config load rather than let PyJWT warn on every encode/decode - so a
        # too-short secret can never reach production silently.
        if len(v.encode("utf-8")) < 32:
            raise ValueError(
                "api.jwt_secret must be at least 32 bytes (HS256 minimum); "
                "set a strong DFE_ENGINE_API_JWT_SECRET"
            )
        return v


# GCP Secret Manager and Azure Key Vault names allow letters, digits, '-' and (GCP only) '_'.
_UNNAMEABLE_SECRET_PROVIDERS = frozenset({"gcp", "azure"})
SecretsProvider = Literal["file", "openbao", "vault", "aws"]
# Amazon MSK reads SCRAM credentials only from secrets whose names start with this.
_MSK_NAME_PREFIX = "AmazonMSK_"
# AWS Secrets Manager's CreateSecret takes these characters in a name, at most 512 of them.
_AWS_SECRET_NAME = re.compile(r"[A-Za-z0-9/_+=.@-]+")
_AWS_SECRET_NAME_MAX = 512
# The longest engine path whose parts are bounded: an OIDC provider's directory key.
_LONGEST_ENGINE_PATH = len("oidc//groups_service_account_json") + MAX_NAME_LENGTH
_NAME_PREFIX_MAX = _AWS_SECRET_NAME_MAX - len("/") - _LONGEST_ENGINE_PATH


class SecretsSettings(BaseModel):
    """Backing-service seam for secrets the engine MINTS - backend by config.

    The engine reads runtime config secrets from env; this is the WRITE seam for
    secrets it generates (the JWT signing key, ClickHouse passwords, OIDC client
    secrets). ``provider`` selects the scalo.secrets backend and NEVER a hardcoded
    product: 'file' by default (a directory on the engine's volume), 'openbao'/'vault'
    where a deployment runs one, and 'aws' for AWS Secrets Manager.
    'gcp' and 'azure' are refused: their secret names allow no '/', and every path
    the engine mints carries one. See docs/deployment/backing-services.md.

    """

    provider: SecretsProvider = Field(default="file", description="scalo.secrets backend provider")
    path: str = Field(default="./.secrets", description="file provider root path")
    addr: str = Field(default="", description="openbao/vault address")
    mount: str = Field(default="dfe", description="kv mount / key prefix")
    role: str = Field(default="", description="openbao AppRole role id")
    region: str = Field(default="", description="aws region the secrets live in")
    # Not DFE_SECRETS_PREFIX, which bootstrap.sh reads as the ESO store root above these names.
    name_prefix: str = Field(
        default="", description="aws name prefix, so a secret is <name_prefix>/<path>"
    )
    kms_key_id: str = Field(
        default="", description="aws KMS key id, ARN or alias, empty for the AWS managed key"
    )
    recovery_window_days: int = Field(
        default=0,
        description="aws days a deleted secret stays recoverable: 0 for no window, or 7-30",
    )
    tags: dict[str, str] = Field(
        default_factory=dict, description="aws tags every secret the engine creates carries"
    )

    @field_validator("tags", mode="before")
    @classmethod
    def _tags_from_json(cls, value: object) -> object:
        # Parsed here, not in the env mapping, so a malformed value fails only a secrets load.
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"DFE_ENGINE_SECRETS_TAGS is not valid JSON: {exc}") from exc

    @field_validator("provider", mode="before")
    @classmethod
    def _nameable_provider(cls, value: object) -> object:
        if value in _UNNAMEABLE_SECRET_PROVIDERS:
            raise ValueError(
                f"DFE_ENGINE_SECRETS_PROVIDER={value} is not supported yet for the secrets "
                "the engine mints: its secret names cannot hold the '/' every engine path "
                "carries. Use file, openbao or aws"
            )
        return value

    @model_validator(mode="after")
    def _check_provider(self) -> SecretsSettings:
        if self.provider == "aws":
            self._check_aws()
        return self

    def _check_aws(self) -> None:
        if not self.region:
            raise ValueError("DFE_ENGINE_SECRETS_PROVIDER=aws needs DFE_ENGINE_SECRETS_REGION")
        self._check_name_prefix(self.name_prefix)
        window = self.recovery_window_days
        if window != 0 and not 7 <= window <= 30:
            raise ValueError(
                "DFE_ENGINE_SECRETS_RECOVERY_WINDOW_DAYS must be 0 (no recovery window) or 7-30, "
                f"got {window}"
            )

    @staticmethod
    def _check_name_prefix(prefix: str) -> None:
        if not prefix:
            raise ValueError(
                "DFE_ENGINE_SECRETS_PROVIDER=aws needs DFE_ENGINE_SECRETS_NAME_PREFIX, so two deployments "
                "in one account never share a secret name"
            )
        if not _AWS_SECRET_NAME.fullmatch(prefix):
            raise ValueError(
                f"DFE_ENGINE_SECRETS_NAME_PREFIX {prefix!r} may hold only ASCII letters, digits "
                "and /_+=.@-, the characters AWS Secrets Manager allows in a name"
            )
        if len(prefix) > _NAME_PREFIX_MAX:
            raise ValueError(
                f"DFE_ENGINE_SECRETS_NAME_PREFIX is {len(prefix)} characters: at most {_NAME_PREFIX_MAX} "
                f"keeps every engine secret name within AWS's {_AWS_SECRET_NAME_MAX}"
            )
        if prefix.startswith("/") or prefix.endswith("/"):
            raise ValueError(
                f"DFE_ENGINE_SECRETS_NAME_PREFIX {prefix!r} must not start or end with '/'"
            )
        if prefix.startswith(_MSK_NAME_PREFIX):
            raise ValueError(
                f"DFE_ENGINE_SECRETS_NAME_PREFIX {prefix!r} must not start with {_MSK_NAME_PREFIX!r}, "
                "which names the secrets Amazon MSK authenticates against"
            )


class SeedOrg(BaseModel):
    """One organisation created at startup when the registry does not already hold it."""

    name: str = Field(description="Org name, the registry identifier", pattern=ORG_NAME_PATTERN)
    display_name: str = Field(default="", description="Human-readable label")
    org_ids: list[TenantId] = Field(
        default_factory=list,
        description="Tenant IDs for ClickHouse row-level security",
    )


class IngestSettings(BaseModel):
    """Tenancy settings the engine compiles into dfe-receiver."""

    source_org_id: Literal["disallow", "allow"] = Field(
        default="disallow",
        description=(
            "Whether a sender may set _org_id, the field that picks the tenant. disallow "
            "strips it before the org rules run; allow keeps it, held to the orgs the "
            "sender's ingest credential binds. Compiled as routing.source_org_id."
        ),
    )
    bearer_secret: str = Field(
        default="",
        description=(
            "Kubernetes Secret, in the engine's own namespace, that the engine also writes each "
            "listener's bearer tokens into, one key per listener, for dfe-receiver to mount. "
            "Empty writes none. The engine needs get and update on that one Secret."
        ),
    )

    @field_validator("bearer_secret")
    @classmethod
    def _bearer_secret_is_a_secret_name(cls, value: str) -> str:
        if value:
            # scalo's own check, so the engine refuses at startup what the API server would.
            from scalo.secrets import KubernetesConfig

            try:
                KubernetesConfig(secret_name=value)
            except ValueError as exc:
                raise ValueError(f"DFE_ENGINE_INGEST_BEARER_SECRET: {exc}") from exc
        return value


class OrgsSettings(BaseModel):
    """Organisation bootstrap config."""

    seed_orgs: list[SeedOrg] = Field(
        default_factory=list,
        description=(
            "Orgs created on startup when absent; an existing org is never changed, "
            "so console edits survive a restart. DFE_ENGINE_ORGS_SEED_ORGS: a JSON list of "
            "{name, display_name, org_ids}."
        ),
    )


class DFESettings(BaseModel):
    """Main DFE Engine settings container."""

    config_dir: str = Field(
        default="",
        description=(
            "Root config directory the deployment supplies: a mounted volume, or a checkout "
            "of the deployment's own config repo. Auto-resolves registry subdirs."
        ),
    )
    clickhouse: ClickHouseSettings = Field(default_factory=ClickHouseSettings)
    hunts: HuntsSettings = Field(default_factory=HuntsSettings)
    artifactory: ArtifactorySettings = Field(default_factory=ArtifactorySettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    transport: TransportSettings = Field(default_factory=TransportSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    query_views: QueryViewSettings = Field(default_factory=QueryViewSettings)
    sampler: SamplerSettings = Field(default_factory=SamplerSettings)
    synthetic_data: SyntheticDataSettings = Field(default_factory=SyntheticDataSettings)
    keda_shim: KedaShimSettings = Field(default_factory=KedaShimSettings)
    schemas: SchemasSettings = Field(default_factory=SchemasSettings)
    source: SourceSettings = Field(default_factory=SourceSettings)
    fieldmap: FieldMapSettings = Field(default_factory=FieldMapSettings)
    sigma: SigmaSettings = Field(default_factory=SigmaSettings)
    services: ServicesSettings = Field(default_factory=ServicesSettings)
    repository: RepositorySettings = Field(default_factory=RepositorySettings)
    deployment: DeploymentSettings = Field(default_factory=DeploymentSettings)
    transform_validation: TransformValidationSettings = Field(
        default_factory=TransformValidationSettings
    )
    auth: AuthSettings = Field(default_factory=AuthSettings)
    orgs: OrgsSettings = Field(default_factory=OrgsSettings)
    ingest: IngestSettings = Field(default_factory=IngestSettings)
    hyperdx: HyperDXSettings = Field(default_factory=HyperDXSettings)
    gitops: GitopsSettings = Field(default_factory=GitopsSettings)
    api: APISettings = Field(default_factory=APISettings)
    secrets: SecretsSettings = Field(default_factory=SecretsSettings)
    stack_version: str = Field(
        default="",
        description=(
            "The certified stack this pod was deployed from, passed by the chart "
            "from the cluster secret. The console falls back to it when the deploy "
            "repo pins no stack. DFE_ENGINE_STACK_VERSION."
        ),
    )
    ui_version: str = Field(
        default="",
        description=(
            "The dfe-ui version this deployment renders, passed by the engine chart "
            "from the same versions.yaml pin the ui chart's appVersion carries. "
            "Helm cannot read a sibling chart, so the engine is told rather than "
            "asked. The deploy repo's pins win where they name one. DFE_ENGINE_UI_VERSION."
        ),
    )
    env: str = Field(
        default="production",
        description=(
            "Deployment posture. 'production' (default, secure) rejects the "
            "placeholder api.jwt_secret when auth is enabled; set DFE_ENGINE_ENV to "
            "dev/development/local/test/ci for local development. DFE_ENGINE_ENV."
        ),
    )
    e2e_server: bool = Field(
        default=False,
        description=(
            "Mount the unauthenticated /api/e2e/* Playwright helpers "
            "(seed-admin). DFE_ENGINE_E2E_SERVER. Refused in a production posture."
        ),
    )

    @model_validator(mode="after")
    def _a_bus_needs_a_broker(self) -> DFESettings:
        # Otherwise the schema phase waits out the dead-letter window on a broker nobody listed.
        if self.transport.bus_present and not self.kafka.bootstrap_servers:
            raise ValueError(
                "transport.bus_present is true but kafka.bootstrap_servers is empty: set "
                "DFE_ENGINE_KAFKA_BOOTSTRAP_SERVERS, or DFE_ENGINE_TRANSPORT_BUS_PRESENT=false and "
                "DFE_ENGINE_TRANSPORT_DEFAULT=direct for a deployment with no broker"
            )
        return self

    @model_validator(mode="after")
    def _data_database_is_no_lookup(self) -> DFESettings:
        # Every org's rows are in the data database, so a lookup there reads them all.
        data = self.clickhouse.effective_data_database
        if data in self.hunts.lookup_databases:
            raise ValueError(
                f"hunts.lookup_databases lists {data!r}, the data database, which holds "
                "every org's rows"
            )
        return self

    @model_validator(mode="after")
    def _dev_cors_origins(self) -> DFESettings:
        # A production console's origin is the deployment's own; localhost is a dev server.
        if is_dev_posture(self.env) and "cors_origins" not in self.api.model_fields_set:
            self.api.cors_origins = list(_DEV_CORS_ORIGINS)
        return self

    @model_validator(mode="after")
    def _reject_insecure_production_posture(self) -> DFESettings:
        # Two ways a production posture can enforce nothing. Both are errors here
        # rather than warnings, because a warning leaves the process running.
        is_prod = not is_dev_posture(self.env)
        if self.e2e_server and is_prod:
            raise ValueError(
                "e2e_server is True but env is production: the unauthenticated "
                "/api/e2e/* seed endpoints must not ship. Set DFE_ENGINE_ENV to "
                "dev/test/ci, or DFE_ENGINE_E2E_SERVER=false"
            )
        if not is_prod:
            return self

        # `auth.enabled: False` is not "no auth configured yet" -- api/deps.py
        # hands an unauthenticated request roles=["admin"], and authorize()
        # short-circuits to allowed=True. `env` defaults to "production" and
        # `auth.enabled` defaults to False, so this pairing was the default one:
        # anonymous admin, with nothing logged and no other validator covering it
        # (the jwt_secret check below is itself gated on auth.enabled, so it could
        # never fire for this case).
        if not self.auth.enabled:
            raise ValueError(
                f"auth.enabled is False but env is '{self.env}': that grants every "
                "unauthenticated caller the admin role. Set DFE_ENGINE_AUTH_ENABLED=true, "
                "or DFE_ENGINE_ENV=dev for local development"
            )

        # A production deployment that turns auth on but never overrode the known
        # dev jwt_secret lets anyone forge tokens. Minimum length is enforced on
        # the field itself.
        if self.api.jwt_secret == _DEV_JWT_SECRET:
            raise ValueError(
                "api.jwt_secret is the known dev placeholder but env is "
                f"'{self.env}' with auth enabled; set a strong DFE_ENGINE_API_JWT_SECRET "
                "(or DFE_ENGINE_ENV=dev for local development)"
            )
        return self


def _load_defaults() -> dict:
    """Load default configuration from defaults.yaml."""
    defaults_path = Path(__file__).parent / "defaults.yaml"
    if defaults_path.exists():
        return yaml_load(defaults_path) or {}
    return {}


def backing_from_environment() -> BackingSettings:
    """The backing-service facts the environment states, parsed as ``load_settings`` parses them.

    Reads the environment alone, which is where a deployer injects these facts; a
    config-file value is not seen here.
    """
    deployment = _get_env_overrides().get("deployment") or {}
    return BackingSettings(**(deployment.get("backing") or {}))


def _bool(value: str) -> bool:
    return value.lower() in ("true", "1", "yes")


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _strip_lower(value: str) -> str:
    return value.strip().lower()


# Each group converts the variable's text with the callable, then sets the dotted field path.
_ENV_FIELDS: tuple[tuple[Callable[[str], Any], dict[str, str]], ...] = (
    (
        str,
        {
            "DFE_ENGINE_CLICKHOUSE_HOST": "clickhouse.host",
            "DFE_ENGINE_CLICKHOUSE_USERNAME": "clickhouse.username",
            "DFE_ENGINE_CLICKHOUSE_PASSWORD": "clickhouse.password",
            "DFE_ENGINE_CLICKHOUSE_DATABASE": "clickhouse.database",
            "DFE_ENGINE_CLICKHOUSE_DATA_DATABASE": "clickhouse.data_database",
            "DFE_ENGINE_DEFAULT_TABLE_PROFILE": "clickhouse.default_table_profile",
            "DFE_ENGINE_CLICKHOUSE_CA_CERT": "clickhouse.ca_cert",
            "DFE_ENGINE_CLICKHOUSE_PLATFORM": "clickhouse.platform",
            "DFE_ENGINE_CLICKHOUSE_CLOUD_API_KEY_ID": "clickhouse.cloud.api_key_id",
            "DFE_ENGINE_CLICKHOUSE_CLOUD_API_KEY_SECRET": "clickhouse.cloud.api_key_secret",
            "DFE_ENGINE_CLICKHOUSE_CLOUD_API_BASE": "clickhouse.cloud.api_base",
            "DFE_ENGINE_CLICKHOUSE_CLOUD_ORGANIZATION_ID": "clickhouse.cloud.organization_id",
            "DFE_ENGINE_CLICKHOUSE_CLOUD_SERVICE_ID": "clickhouse.cloud.service_id",
            "DFE_ENGINE_CLICKHOUSE_CLOUD_SERVICE": "clickhouse.cloud.service_name",
            "DFE_ENGINE_HUNTS_DIR": "hunts.hunt_dir",
            "DFE_ENGINE_HUNTS_RULES_DIR": "hunts.rules_dir",
            "DFE_ENGINE_HUNTS_CHECKPOINT_TIMESTAMP_FIELD": "hunts.checkpoint_timestamp_field",
            "DFE_ENGINE_HUNTS_ALERT_DESTINATIONS_DIR": "hunts.alert_destinations_dir",
            "DFE_ENGINE_ARTIFACTORY_URL": "artifactory.url",
            "DFE_ENGINE_ARTIFACTORY_USERNAME": "artifactory.username",
            "DFE_ENGINE_ARTIFACTORY_PASSWORD": "artifactory.password",
            "DFE_ENGINE_TEMPLATES_VERSION": "artifactory.templates_version",
            "DFE_ENGINE_KAFKA_PROVIDER": "kafka.provider",
            "DFE_ENGINE_KAFKA_EXTERNAL_BOOTSTRAP_SERVERS": "kafka.external_bootstrap_servers",
            "DFE_ENGINE_KAFKA_SECURITY_PROTOCOL": "kafka.security_protocol",
            "DFE_ENGINE_KAFKA_SASL_MECHANISM": "kafka.sasl_mechanism",
            "DFE_ENGINE_KAFKA_SASL_USERNAME": "kafka.sasl_username",
            "DFE_ENGINE_KAFKA_SASL_PASSWORD": "kafka.sasl_password",
            "DFE_ENGINE_KAFKA_TOPIC_CLEANUP_POLICY": "kafka.topic_cleanup_policy",
            "DFE_ENGINE_KAFKA_TOPIC_COMPRESSION_TYPE": "kafka.topic_compression_type",
            "DFE_ENGINE_TRANSPORT_DEFAULT": "transport.default",
            "DFE_ENGINE_MESH_NAMESPACE": "transport.mesh_namespace",
            "DFE_ENGINE_STORAGE_TYPE": "storage.type",
            "DFE_ENGINE_STORAGE_PATH": "storage.path",
            "DFE_ENGINE_S3_REGION": "storage.s3_region",
            "DFE_ENGINE_QUERY_VIEWS_RESTRICTED_USER": "query_views.restricted_user",
            "DFE_ENGINE_QUERY_VIEWS_RESTRICTED_PASSWORD": "query_views.restricted_password",
            "DFE_ENGINE_SAMPLER_DEFAULT_MODE": "sampler.default_mode",
            "DFE_ENGINE_SAMPLER_LEVEL": "sampler.level",
            "DFE_ENGINE_SAMPLER_TIMESTAMP_FIELD": "sampler.timestamp_field",
            "DFE_ENGINE_SYNTHETIC_DATA_AUTOSTART_SCHEMAS": "synthetic_data.autostart_schemas",
            "DFE_ENGINE_SYNTHETIC_DATA_AUTOSTART_RECEIVER_URL": "synthetic_data.autostart_receiver_url",
            "DFE_ENGINE_SYNTHETIC_DATA_ALLOWED_RECEIVER_HOSTS": "synthetic_data.allowed_receiver_hosts",
            "DFE_ENGINE_KEDA_SHIM_HOST": "keda_shim.host",
            "DFE_ENGINE_KEDA_SHIM_QUERY_CONFIG": "keda_shim.query_config",
            "DFE_ENGINE_SCHEMAS_DIR": "schemas.schemas_dir",
            "DFE_ENGINE_E2E_SCHEMA_FIXTURES_DIR": "schemas.e2e_schema_fixtures_dir",
            "DFE_ENGINE_SOURCES_DIR": "source.sources_dir",
            "DFE_ENGINE_SOURCE_BUILDS_DIR": "source.builds_dir",
            "DFE_ENGINE_SOURCE_PLANS_DIR": "source.plans_dir",
            "DFE_ENGINE_SOURCE_DEPLOYS_DIR": "source.deploys_dir",
            "DFE_ENGINE_FIELDMAPS_DIR": "fieldmap.fieldmaps_dir",
            "DFE_ENGINE_SERVICES_TRANSFORM_WASM_URL": "services.transform_wasm_url",
            "DFE_ENGINE_SERVICES_TRANSFORM_WASM_COMPILER_URL": "services.transform_wasm_compiler_url",
            "DFE_ENGINE_SERVICES_CONFIG_YAML_DIR": "services.config_yaml_dir",
            "DFE_ENGINE_DEPLOYMENT_NAMESPACE": "deployment.namespace",
            "DFE_ENGINE_AUTH_DIR": "auth.auth_dir",
            "DFE_ENGINE_AUTH_PROXY_PROVIDER": "auth.proxy_provider",
            "DFE_ENGINE_AUTH_LOCAL_ADMIN_NAME": "auth.local.admin_name",
            "DFE_ENGINE_AUTH_LOCAL_ADMIN_PASSWORD": "auth.local.admin_password",
            "DFE_ENGINE_AUTH_BREAKGLASS_PASSWORD": "auth.local.breakglass_password",
            "DFE_ENGINE_RECOVERY_EMAIL": "auth.local.recovery_email",
            "DFE_ENGINE_AUTH_LOCAL_ADMIN_PASSWORD_SECRET_PATH": "auth.local.admin_password_secret_path",
            "DFE_ENGINE_AUTH_LOCAL_ADMIN_SECRET_NAME": "auth.local.admin_secret_name",
            "DFE_ENGINE_AUTH_LOCAL_ADMIN_SECRET_KEY": "auth.local.admin_secret_key",
            "DFE_ENGINE_HYPERDX_BASE_URL": "hyperdx.base_url",
            "DFE_ENGINE_GITOPS_REPO_URL": "gitops.repo_url",
            "DFE_ENGINE_GITOPS_BRANCH": "gitops.branch",
            "DFE_ENGINE_GITOPS_LOCAL_PATH": "gitops.local_path",
            "DFE_ENGINE_GITOPS_USERNAME": "gitops.username",
            "DFE_ENGINE_GITOPS_TOKEN": "gitops.token",
            "DFE_ENGINE_GITOPS_AUTHOR_NAME": "gitops.author_name",
            "DFE_ENGINE_GITOPS_AUTHOR_EMAIL": "gitops.author_email",
            "DFE_ENGINE_GITOPS_FORGE_API_BASE": "gitops.forge_api_base",
            "DFE_ENGINE_API_HOST": "api.host",
            "DFE_ENGINE_API_JWT_SECRET": "api.jwt_secret",
            "DFE_ENGINE_API_SESSION_SECRET": "api.session_secret",
            "DFE_ENGINE_SECRETS_PROVIDER": "secrets.provider",
            "DFE_ENGINE_SECRETS_PATH": "secrets.path",
            "DFE_ENGINE_SECRETS_ADDR": "secrets.addr",
            "DFE_ENGINE_SECRETS_MOUNT": "secrets.mount",
            "DFE_ENGINE_SECRETS_ROLE": "secrets.role",
            "DFE_ENGINE_SECRETS_REGION": "secrets.region",
            "DFE_ENGINE_SECRETS_NAME_PREFIX": "secrets.name_prefix",
            "DFE_ENGINE_SECRETS_KMS_KEY_ID": "secrets.kms_key_id",
            "DFE_ENGINE_SECRETS_RECOVERY_WINDOW_DAYS": "secrets.recovery_window_days",
            "DFE_ENGINE_SECRETS_TAGS": "secrets.tags",
            "DFE_ENGINE_STACK_VERSION": "stack_version",
            "DFE_ENGINE_UI_VERSION": "ui_version",
            "DFE_ENGINE_ENV": "env",
        },
    ),
    (
        int,
        {
            "DFE_ENGINE_CLICKHOUSE_PORT": "clickhouse.port",
            "DFE_ENGINE_CLICKHOUSE_CONNECTIONS_MAX": "clickhouse.connections_max",
            "DFE_ENGINE_CLICKHOUSE_DDL_MAX_QUERY_SIZE": "clickhouse.ddl_max_query_size",
            "DFE_ENGINE_CLICKHOUSE_DEFAULT_TTL_DAYS": "clickhouse.default_ttl_days",
            "DFE_ENGINE_HUNTS_MAX_DETECTIONS_PER_RUN": "hunts.max_detections_per_run",
            "DFE_ENGINE_HUNTS_MAX_DETECTIONS_PER_RUN_CEILING": "hunts.max_detections_per_run_ceiling",
            "DFE_ENGINE_HUNTS_WINDOW_OVERLAP_SECONDS": "hunts.window_overlap_seconds",
            "DFE_ENGINE_HUNTS_SAFE_HORIZON_MAX_DEFERRAL_SECONDS": "hunts.safe_horizon_max_deferral_seconds",
            "DFE_ENGINE_DETECTION_GUARD_PREVIEW_WINDOW_MINUTES": "hunts.detection_guard.preview_window_minutes",
            "DFE_ENGINE_DETECTION_GUARD_PREVIEW_MAX_ROWS": "hunts.detection_guard.preview_max_rows",
            "DFE_ENGINE_DETECTION_GUARD_WARN_PER_DAY": "hunts.detection_guard.warn_per_day",
            "DFE_ENGINE_DETECTION_GUARD_ACK_PER_DAY": "hunts.detection_guard.ack_per_day",
            "DFE_ENGINE_DETECTION_GUARD_BLOCK_PER_DAY": "hunts.detection_guard.block_per_day",
            "DFE_ENGINE_DETECTION_GUARD_MIN_ROWS_FOR_RATIO": "hunts.detection_guard.min_rows_for_ratio",
            "DFE_ENGINE_KAFKA_TOPIC_PARTITIONS": "kafka.topic_partitions",
            "DFE_ENGINE_KAFKA_TOPIC_REPLICATION_FACTOR": "kafka.topic_replication_factor",
            "DFE_ENGINE_KAFKA_TOPIC_RETENTION_MS": "kafka.topic_retention_ms",
            "DFE_ENGINE_KAFKA_TOPIC_MAX_MESSAGE_BYTES": "kafka.topic_max_message_bytes",
            "DFE_ENGINE_QUERY_VIEWS_MAX_EXECUTION_TIME": "query_views.max_execution_time",
            "DFE_ENGINE_QUERY_VIEWS_MAX_ROWS_TO_READ": "query_views.max_rows_to_read",
            "DFE_ENGINE_QUERY_VIEWS_MAX_MEMORY_USAGE": "query_views.max_memory_usage",
            "DFE_ENGINE_SAMPLER_DEFAULT_LIMIT": "sampler.default_limit",
            "DFE_ENGINE_SAMPLER_MAX_LIMIT": "sampler.max_limit",
            "DFE_ENGINE_SAMPLER_MAX_CONCURRENT": "sampler.max_concurrent",
            "DFE_ENGINE_SAMPLER_MAX_SCAN_ROWS": "sampler.max_scan_rows",
            "DFE_ENGINE_SAMPLER_KAFKA_MAX_MESSAGES": "sampler.kafka_max_messages",
            "DFE_ENGINE_SAMPLER_MAX_EXECUTION_TIME": "sampler.max_execution_time",
            "DFE_ENGINE_SYNTHETIC_DATA_MAX_COUNT": "synthetic_data.max_count",
            "DFE_ENGINE_SYNTHETIC_DATA_AUTOSTART_SEED": "synthetic_data.autostart_seed",
            "DFE_ENGINE_SYNTHETIC_DATA_MAX_CONCURRENT_STREAMS": "synthetic_data.max_concurrent_streams",
            "DFE_ENGINE_KEDA_SHIM_PORT": "keda_shim.port",
            "DFE_ENGINE_REPOSITORY_MAX_PREFS_BYTES": "repository.max_prefs_bytes",
            "DFE_ENGINE_REPOSITORY_MAX_OBJECT_BYTES": "repository.max_object_bytes",
            "DFE_ENGINE_AUTH_API_KEY_DEFAULT_TTL_DAYS": "auth.api_key_default_ttl_days",
            "DFE_ENGINE_AUTH_OIDC_GROUP_SYNC_TICK_SECONDS": "auth.oidc_group_sync_tick_seconds",
            "DFE_ENGINE_AUTH_LOGIN_THROTTLE_USERNAME_FAILURES": "auth.login_throttle.username_failures",
            "DFE_ENGINE_AUTH_LOGIN_THROTTLE_CLIENT_FAILURES": "auth.login_throttle.client_failures",
            "DFE_ENGINE_AUTH_LOGIN_THROTTLE_MAX_DELAY_SECONDS": "auth.login_throttle.max_delay_seconds",
            "DFE_ENGINE_GITOPS_WRITE_FAILURE_THRESHOLD": "gitops.write.failure_threshold",
            "DFE_ENGINE_GITOPS_PACK_LOOSE_OBJECTS": "gitops.pack.loose_objects",
            "DFE_ENGINE_GITOPS_PACK_PACKS": "gitops.pack.packs",
            "DFE_ENGINE_API_PORT": "api.port",
            "DFE_ENGINE_API_JWT_EXPIRE_MINUTES": "api.jwt_expire_minutes",
            "DFE_ENGINE_API_MAX_SESSION_MINUTES": "api.max_session_minutes",
            "DFE_ENGINE_API_MAX_ACTIVE_TASKS": "api.max_active_tasks",
            "DFE_ENGINE_API_MAX_REQUEST_BODY_BYTES": "api.max_request_body_bytes",
            "DFE_ENGINE_API_ELASTIC_CONVERTER_MAX_UPLOAD_BYTES": "api.elastic_converter_max_upload_bytes",
            "DFE_ENGINE_API_ELASTIC_CONVERTER_READ_CHUNK_SIZE": "api.elastic_converter_read_chunk_size",
            "DFE_ENGINE_API_ELASTIC_CONVERTER_CONTENT_LENGTH_SLACK_BYTES": "api.elastic_converter_content_length_slack_bytes",
        },
    ),
    (
        float,
        {
            "DFE_ENGINE_CLICKHOUSE_BOOTSTRAP_WAIT_SECONDS": "clickhouse.bootstrap_wait_seconds",
            "DFE_ENGINE_DETECTION_GUARD_PREVIEW_TIMEOUT_SECONDS": "hunts.detection_guard.preview_timeout_seconds",
            "DFE_ENGINE_DETECTION_GUARD_WARN_MATCH_RATIO": "hunts.detection_guard.warn_match_ratio",
            "DFE_ENGINE_DETECTION_GUARD_BLOCK_MATCH_RATIO": "hunts.detection_guard.block_match_ratio",
            "DFE_ENGINE_SAMPLER_MAX_MEMORY_GB": "sampler.max_memory_gb",
            "DFE_ENGINE_SAMPLER_WAIT_SECONDS": "sampler.wait_seconds",
            "DFE_ENGINE_SYNTHETIC_DATA_MAX_RATE_EPS": "synthetic_data.max_rate_eps",
            "DFE_ENGINE_SYNTHETIC_DATA_MAX_STREAM_SECONDS": "synthetic_data.max_stream_seconds",
            "DFE_ENGINE_SYNTHETIC_DATA_DEFAULT_RATE_EPS": "synthetic_data.default_rate_eps",
            "DFE_ENGINE_SYNTHETIC_DATA_AUTOSTART_RATE_EPS": "synthetic_data.autostart_rate_eps",
            "DFE_ENGINE_AUTH_OIDC_LOGIN_DIRECTORY_DEADLINE_SECONDS": "auth.oidc_login_directory_deadline_seconds",
            "DFE_ENGINE_GITOPS_READ_TIMEOUT_SECONDS": "gitops.read.timeout_seconds",
            "DFE_ENGINE_GITOPS_WRITE_TIMEOUT_SECONDS": "gitops.write.timeout_seconds",
            "DFE_ENGINE_GITOPS_WRITE_BUDGET_SECONDS": "gitops.write.budget_seconds",
            "DFE_ENGINE_GITOPS_WRITE_RESET_TIMEOUT": "gitops.write.reset_timeout",
            "DFE_ENGINE_GITOPS_PACK_INTERVAL_SECONDS": "gitops.pack.interval_seconds",
        },
    ),
    (
        _bool,
        {
            "DFE_ENGINE_CLICKHOUSE_BOOTSTRAP_TABLES": "clickhouse.bootstrap_tables",
            "DFE_ENGINE_CLICKHOUSE_SECURE": "clickhouse.secure",
            "DFE_ENGINE_CLICKHOUSE_VERIFY": "clickhouse.verify",
            "DFE_ENGINE_CLICKHOUSE_CLOUD_AUTOWAKE": "clickhouse.cloud.autowake",
            "DFE_ENGINE_HUNTS_SAFE_HORIZON": "hunts.safe_horizon",
            "DFE_ENGINE_DETECTION_GUARD_ENABLED": "hunts.detection_guard.enabled",
            "DFE_ENGINE_KAFKA_ENSURE_TOPICS": "kafka.ensure_topics",
            "DFE_ENGINE_KAFKA_BOOTSTRAP_TOPICS": "kafka.bootstrap_topics",
            "DFE_ENGINE_KAFKA_TIERED_STORAGE": "kafka.tiered_storage",
            "DFE_ENGINE_TRANSPORT_BUS_PRESENT": "transport.bus_present",
            "DFE_ENGINE_MESH_ENABLED": "transport.mesh_enabled",
            "DFE_ENGINE_SOURCE_ALLOW_MIXED_FETCHER_TYPES": "source.allow_mixed_fetcher_types",
            "DFE_ENGINE_SIGMA_ENABLED": "sigma.enabled",
            "DFE_ENGINE_TRANSFORM_VALIDATION_ENABLED": "transform_validation.enabled",
            "DFE_ENGINE_TRANSFORM_VALIDATION_BLOCKING": "transform_validation.blocking",
            "DFE_ENGINE_TRANSFORM_DRY_RUN_ENABLED": "transform_validation.dry_run",
            "DFE_ENGINE_TRANSFORM_ALLOW_HOST_FUNCTIONS": "transform_validation.allow_host_functions",
            "DFE_ENGINE_AUTH_ENABLED": "auth.enabled",
            "DFE_ENGINE_AUTH_TRUST_PROXY_AUTH_HEADERS": "auth.trust_proxy_auth_headers",
            "DFE_ENGINE_AUTH_OIDC_GROUP_SYNC_ENABLED": "auth.oidc_group_sync_enabled",
            "DFE_ENGINE_AUTH_LOCAL_ENABLED": "auth.local.enabled",
            "DFE_ENGINE_AUTH_LOGIN_THROTTLE_ENABLED": "auth.login_throttle.enabled",
            "DFE_ENGINE_HYPERDX_ENABLED": "hyperdx.enabled",
            "DFE_ENGINE_GITOPS_ENABLED": "gitops.enabled",
            "DFE_ENGINE_GITOPS_PUSH": "gitops.push",
            "DFE_ENGINE_API_DOCS_ENABLED": "api.docs_enabled",
            "DFE_ENGINE_E2E_SERVER": "e2e_server",
        },
    ),
    (
        str.strip,
        {
            "DFE_ENGINE_PROFILE": "deployment.profile",
            "DFE_ENGINE_ADMIN_LINKS": "deployment.admin_links",
            "DFE_ENGINE_DEPLOYMENT_APP_CONFIG_DIR": "deployment.app_config_dir",
            "DFE_ENGINE_DEPLOYMENT_APP_CONFIG_BASE_DIR": "deployment.app_config_base_dir",
            "DFE_ENGINE_DEPLOYMENT_APP_CONFIG_MOUNT": "deployment.app_config_mount",
            "DFE_ENGINE_DEPLOYMENT_APP_ENV_DIR": "deployment.app_env_dir",
            "DFE_ENGINE_DEPLOYMENT_KAFKA_PROVIDER": "deployment.backing.kafka.provider",
            "DFE_ENGINE_DEPLOYMENT_KAFKA_TIER": "deployment.backing.kafka.tier",
            "DFE_ENGINE_DEPLOYMENT_KAFKA_REGION": "deployment.backing.kafka.region",
            "DFE_ENGINE_DEPLOYMENT_CLICKHOUSE_PROVIDER": "deployment.backing.clickhouse.provider",
            "DFE_ENGINE_DEPLOYMENT_CLICKHOUSE_TIER": "deployment.backing.clickhouse.tier",
            "DFE_ENGINE_DEPLOYMENT_CLICKHOUSE_REGION": "deployment.backing.clickhouse.region",
            "DFE_ENGINE_INGEST_BEARER_SECRET": "ingest.bearer_secret",
            "DFE_ENGINE_API_FORWARDED_ALLOW_IPS": "api.forwarded_allow_ips",
        },
    ),
    (
        str.lower,
        {
            "DFE_ENGINE_INGEST_SOURCE_ORG_ID": "ingest.source_org_id",
        },
    ),
    (
        _strip_lower,
        {
            "DFE_ENGINE_DEPLOYMENT_TARGET": "deployment.target",
            "DFE_ENGINE_GITOPS_MODE": "gitops.mode",
            "DFE_ENGINE_GITOPS_FORGE_PROVIDER": "gitops.forge_provider",
        },
    ),
    (
        _csv,
        {
            "DFE_ENGINE_HUNTS_ALERT_CHANNELS": "hunts.alert_channels",
            "DFE_ENGINE_HUNTS_LOOKUP_DATABASES": "hunts.lookup_databases",
            "DFE_ENGINE_API_CORS_ORIGINS": "api.cors_origins",
        },
    ),
)

# Variables holding JSON. A malformed value fails the load, because dropping it would strip
# the seeded logins or lock out the bound users.
_ENV_JSON_FIELDS = {
    "DFE_ENGINE_AUTH_SOURCE_PROVIDER_BINDINGS": "auth.source_provider_bindings",
    "DFE_ENGINE_AUTH_LOCAL_SEED_ACCOUNTS": "auth.local.seed_accounts",
    "DFE_ENGINE_ORGS_SEED_ORGS": "orgs.seed_orgs",
}

# Registry directories DFE_CONFIG_DIR fills, as <config dir>/<subdir>, when the environment names none.
_CONFIG_DIR_SUBDIRS = {
    "services.config_yaml_dir": "services",
    "source.sources_dir": "sources",
    "source.builds_dir": "source-builds",
    "source.plans_dir": "source-plans",
    "source.deploys_dir": "source-deploys",
    "fieldmap.fieldmaps_dir": "fieldmaps",
    "hunts.hunt_dir": "hunts",
    "hunts.rules_dir": "rules",
    "hunts.alert_destinations_dir": "alert-destinations",
}


def _set_path(tree: dict[str, Any], path: str, value: Any) -> None:
    *parents, leaf = path.split(".")
    for key in parents:
        tree = tree.setdefault(key, {})
    tree[leaf] = value


def _get_env_overrides() -> dict:
    """Get configuration overrides from environment variables."""
    overrides: dict[str, Any] = {}

    for convert, fields in _ENV_FIELDS:
        for name, path in fields.items():
            if value := os.getenv(name):
                _set_path(overrides, path, convert(value))
    for name, path in _ENV_JSON_FIELDS.items():
        if value := os.getenv(name):
            try:
                _set_path(overrides, path, json.loads(value))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{name} is not valid JSON: {exc}") from exc

    # An empty list is the operator saying there is no broker, so it overrides the dev default.
    if (servers := os.environ.get("DFE_ENGINE_KAFKA_BOOTSTRAP_SERVERS")) is not None:
        _set_path(overrides, "kafka.bootstrap_servers", servers)
    # JSON of {name: apprise_url}. A malformed value is ignored.
    if value := os.getenv("DFE_ENGINE_HUNTS_ALERT_DESTINATIONS"):
        with contextlib.suppress(json.JSONDecodeError):
            _set_path(overrides, "hunts.alert_destinations", json.loads(value))

    # The TLS escape valves are scalo's: one setting relaxes every scalo-minted client (ClickHouse,
    # scalo.http, scalo.secrets), and an explicit SCALO_* variable wins.
    if value := os.getenv("DFE_ENGINE_TLS_VERIFY"):
        os.environ.setdefault("SCALO_TLS_VERIFY", value)
    if value := os.getenv("DFE_ENGINE_TLS_ALLOW_WEAK"):
        os.environ.setdefault("SCALO_TLS_ALLOW_WEAK", value)

    if config_dir := os.getenv("DFE_CONFIG_DIR"):
        overrides["config_dir"] = config_dir
        for path, subdir in _CONFIG_DIR_SUBDIRS.items():
            section, key = path.split(".")
            if key not in overrides.get(section, {}):
                _set_path(overrides, path, str(Path(config_dir) / subdir))

    return {section: value for section, value in overrides.items() if value}


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dicts. Override wins on conflicts, and a list it sets is the whole list."""
    import copy

    from dfe_engine.yaml_utils import deep_merge

    result = copy.deepcopy(base)
    deep_merge(result, override)
    return result


def load_settings(config_file: str | None = None) -> DFESettings:
    """Load DFE settings, highest precedence first.

    1. Environment variables (highest priority)
    2. User config file (if provided)
    3. defaults.yaml (lowest priority)

    Args:
        config_file: Optional path to a YAML configuration file

    Returns:
        DFESettings: Validated settings object
    """
    from dfe_engine.env_files import load_env_files
    from dfe_engine.ssl_ca import ensure_platform_ssl_ca_bundle

    load_env_files()
    ensure_platform_ssl_ca_bundle()

    # Start with defaults
    config = _load_defaults()

    # Merge user config file if provided
    if config_file:
        config_path = Path(config_file)
        if config_path.exists():
            user_config = yaml_load(config_path) or {}
            config = _deep_merge(config, user_config)

    # Apply environment variable overrides
    env_overrides = _get_env_overrides()
    config = _deep_merge(config, env_overrides)

    return DFESettings(**config)


def load_clickhouse_settings(config_file: str | None = None) -> ClickHouseSettings:
    """Just the ClickHouse section, off the same cascade as ``load_settings``.

    ``DFESettings`` carries cross-field validators for the API posture -- auth
    must be on outside dev, and the dev jwt_secret placeholder is rejected. They
    are correct for the service and fatal for a tool that never serves a
    request: ``dfe-schema`` connects to ClickHouse and applies DDL, so loading
    the whole model made an unset ``DFE_ENGINE_API_JWT_SECRET`` fail a schema job that
    has no API surface to secure.

    Narrowing the load is the fix rather than relaxing the validators, which
    exist because the insecure pairing used to be the DEFAULT one. Anything that
    only touches ClickHouse should come through here.
    """
    from dfe_engine.env_files import load_env_files
    from dfe_engine.ssl_ca import ensure_platform_ssl_ca_bundle

    load_env_files()
    ensure_platform_ssl_ca_bundle()

    config = _load_defaults()
    if config_file:
        config_path = Path(config_file)
        if config_path.exists():
            config = _deep_merge(config, yaml_load(config_path) or {})
    config = _deep_merge(config, _get_env_overrides())

    return ClickHouseSettings(**(config.get("clickhouse") or {}))


def default_data_database() -> str:
    """The DFE database name, read from the model default.

    THE one place the name ``dfe`` is written is
    ``ClickHouseSettings.data_database``; every module that needs a fallback
    reads it through here rather than re-typing the literal, so the default
    moves in one edit. A live caller passes
    ``settings.clickhouse.effective_data_database`` instead -- this is only the
    no-settings fallback.
    """
    return str(ClickHouseSettings.model_fields["data_database"].default)


def provided_service_password(role: str) -> str:
    """The password a deployment supplies for a minted ClickHouse service user, or "".

    Read from ``DFE_ENGINE_CLICKHOUSE_<ROLE>_PASSWORD`` and never from a config file,
    because config files live in the deployment's git repo. Not stripped: the pod
    that connects as the user reads the same Secret verbatim, and a password that
    differs by one byte fails ClickHouse auth.

    Args:
        role: The service role name, e.g. ``hunt_runner``.

    Returns:
        The provided password, or ``""`` when the deployment supplies none.
    """
    return os.getenv(f"DFE_ENGINE_CLICKHOUSE_{role.upper()}_PASSWORD") or ""


def get_clickhouse_config(settings: DFESettings | None = None) -> dict:
    """Get ClickHouse configuration as a dictionary.

    This provides backward compatibility with existing code that expects
    a dictionary with keys like 'ch_host', 'ch_port', etc.

    Args:
        settings: Optional DFESettings object. If not provided, loads settings.

    Returns:
        dict: ClickHouse configuration dictionary
    """
    if settings is None:
        settings = load_settings()

    return {
        "ch_host": settings.clickhouse.host,
        "ch_port": settings.clickhouse.port,
        "ch_username": settings.clickhouse.username,
        "ch_password": settings.clickhouse.password,
        "ch_database": settings.clickhouse.database,
        "ch_secure": settings.clickhouse.secure,
        "ch_verify": settings.clickhouse.verify,
        "ch_ca_cert": settings.clickhouse.ca_cert,
    }


# Global settings instance (lazy loaded)
_settings: DFESettings | None = None


def get_settings() -> DFESettings:
    """Get the global settings instance, loading if necessary."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings() -> None:
    """Reset the global settings instance (useful for testing)."""
    global _settings
    _settings = None
