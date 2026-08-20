#  Project:      dfe-engine
#  File:         settings.py
#  Purpose:      Centralized configuration management with Pydantic
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""
DFE Engine Settings Module

Provides centralized configuration management using hyperi-pylib settings cascade.
Configuration priority: Environment Variables > Config Files > Defaults

Environment variable mapping (DFE_ prefixed, with legacy fallbacks):

Config Directory:
- DFE_CONFIG_DIR -> config_dir (auto-resolves registry subdirectories)

ClickHouse:
- DFE_CLICKHOUSE_HOST (legacy: CLICKHOUSE_HOST) -> clickhouse.host
- DFE_CLICKHOUSE_PORT (legacy: CLICKHOUSE_PORT) -> clickhouse.port
- DFE_CLICKHOUSE_USERNAME (legacy: CLICKHOUSE_USER) -> clickhouse.username
- DFE_CLICKHOUSE_PASSWORD (legacy: CLICKHOUSE_PASSWORD) -> clickhouse.password
- DFE_CLICKHOUSE_DATABASE (legacy: CLICKHOUSE_DATABASE) -> clickhouse.database
- DFE_CLICKHOUSE_DATA_DATABASE (legacy: CLICKHOUSE_DATA_DATABASE) -> clickhouse.data_database
- DFE_CLICKHOUSE_LANDING_TABLE (legacy: CLICKHOUSE_LANDING_TABLE) -> clickhouse.landing_table
- DFE_CLICKHOUSE_SECURE (legacy: CLICKHOUSE_SECURE) -> clickhouse.secure (true/false)
- DFE_CLICKHOUSE_VERIFY (legacy: CLICKHOUSE_VERIFY) -> clickhouse.verify (true/false)
- DFE_CLICKHOUSE_CONNECTIONS_MIN -> clickhouse.connections_min
- DFE_CLICKHOUSE_CONNECTIONS_MAX -> clickhouse.connections_max

ClickHouse Cloud (control plane; opt-in, billable):
- DFE_CLICKHOUSE_CLOUD_API_KEY_ID -> clickhouse.cloud.api_key_id
- DFE_CLICKHOUSE_CLOUD_API_KEY_SECRET -> clickhouse.cloud.api_key_secret
- DFE_CLICKHOUSE_CLOUD_API_BASE -> clickhouse.cloud.api_base
- DFE_CLICKHOUSE_CLOUD_ORGANIZATION_ID -> clickhouse.cloud.organization_id
- DFE_CLICKHOUSE_CLOUD_SERVICE_ID -> clickhouse.cloud.service_id
- DFE_CLICKHOUSE_CLOUD_SERVICE (or _SERVICE_NAME) -> clickhouse.cloud.service_name
- DFE_CLICKHOUSE_CLOUD_AUTOWAKE -> clickhouse.cloud.autowake (true/false)

Hunts:
- DFE_HUNT_LOG_PATH -> hunts.log_path
- DFE_HUNTS_DIR -> hunts.hunt_dir
- DFE_HUNTS_RULE_REPO_DIR -> hunts.rule_repo_dir
- DFE_HUNTS_RULES_DIR -> hunts.rules_dir
- DFE_HUNTS_NUM_THREADS -> hunts.num_threads
- DFE_HUNTS_CHECKPOINT_DESTINATION -> hunts.checkpoint_destination
- DFE_HUNTS_CHECKPOINT_TIMESTAMP_FIELD -> hunts.checkpoint_timestamp_field
- DFE_HUNTS_CHECKPOINT_PATH -> hunts.checkpoint_path
- DFE_HUNTS_CRON_TASK_TIMEOUT -> hunts.cron_task_timeout
- DFE_HUNTS_JITTER_SECONDS -> hunts.jitter_seconds
- DFE_HUNTS_ALERT_DESTINATIONS -> hunts.alert_destinations (JSON {name: apprise_url})
- DFE_HUNTS_ALERT_DESTINATIONS_DIR -> hunts.alert_destinations_dir

Artifactory:
- DFE_ARTIFACTORY_URL -> artifactory.url
- DFE_ARTIFACTORY_USERNAME -> artifactory.username
- DFE_ARTIFACTORY_PASSWORD -> artifactory.password
- DFE_TEMPLATES_VERSION -> artifactory.templates_version

Kafka:
- DFE_KAFKA_PROVIDER -> kafka.provider (DERIVES security_protocol + sasl_mechanism
  per the credential contract; deploys set this, never the mechanism)
- DFE_KAFKA_BOOTSTRAP_SERVERS (legacy: KAFKA_BOOTSTRAP_SERVERS) -> kafka.bootstrap_servers
- DFE_KAFKA_SECURITY_PROTOCOL (legacy: KAFKA_SECURITY_PROTOCOL) -> kafka.security_protocol

Redpanda Cloud lifecycle (control plane; opt-in, WS-C dfe-engine#99):
- DFE_REDPANDA_API_KEY -> kafka.redpanda_cloud.client_id (OAuth2 client id)
- DFE_REDPANDA_API_SECRET -> kafka.redpanda_cloud.client_secret (OAuth2 client secret)
- DFE_REDPANDA_CLOUD_AUTH_URL -> kafka.redpanda_cloud.auth_url
- DFE_REDPANDA_CLOUD_AUDIENCE -> kafka.redpanda_cloud.audience
- DFE_REDPANDA_CLOUD_API_BASE -> kafka.redpanda_cloud.api_base
- DFE_REDPANDA_CLOUD_RESOURCE_GROUP -> kafka.redpanda_cloud.resource_group
- DFE_REDPANDA_CLOUD_CLUSTER_NAME -> kafka.redpanda_cloud.cluster_name
- DFE_REDPANDA_CLOUD_KAFKA_USER -> kafka.redpanda_cloud.kafka_user

Schemas:
- DFE_SCHEMAS_DIR -> schemas.schemas_dir (dfe-schemas submodule root)

Auth (local):
- DFE_AUTH_LOCAL_ENABLED -> auth.local.enabled
- DFE_AUTH_LOCAL_ADMIN_NAME -> auth.local.admin_name
- DFE_AUTH_LOCAL_ADMIN_PASSWORD -> auth.local.admin_password
- DFE_AUTH_LOCAL_OPERATOR_PASSWORD -> auth.local.operator_password
- DFE_AUTH_LOCAL_VIEWER_PASSWORD -> auth.local.viewer_password
- DFE_AUTH_LOCAL_ORG_ID -> auth.local.org_id

Repository (scope-aligned small-object store):
- DFE_REPOSITORY_DATABASE -> repository.database
- DFE_REPOSITORY_MAX_PREFS_BYTES -> repository.max_prefs_bytes
- DFE_REPOSITORY_MAX_OBJECT_BYTES -> repository.max_object_bytes

Storage:
- DFE_STORAGE_TYPE -> storage.type (local, s3, http - auto-detected from path if not set)
- DFE_STORAGE_PATH -> storage.path (local path, S3 URI, or HTTP URL)
- DFE_S3_BUCKET -> storage.s3_bucket
- DFE_S3_REGION -> storage.s3_region

API (Elasticsearch template elastic-converter upload limits):
- DFE_API_ELASTIC_CONVERTER_MAX_UPLOAD_BYTES -> api.elastic_converter_max_upload_bytes
- DFE_API_ELASTIC_CONVERTER_READ_CHUNK_SIZE -> api.elastic_converter_read_chunk_size
- DFE_API_ELASTIC_CONVERTER_CONTENT_LENGTH_SLACK_BYTES ->
  api.elastic_converter_content_length_slack_bytes
"""

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .yaml_utils import yaml_load


def _get_env(primary: str, *fallbacks: str) -> str | None:
    """Get env var with fallback support. Primary (DFE_*) takes precedence over legacy names."""
    if val := os.getenv(primary):
        return val
    for fallback in fallbacks:
        if val := os.getenv(fallback):
            return val
    return None


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
    port: int = Field(default=9000)
    username: str = Field(default="default")
    password: str = Field(default="")
    database: str = Field(
        default="default",
        description="Database the client connection authenticates against (its default db)",
    )
    data_database: str = Field(
        default="dfe",
        description=(
            "Database where DFE data tables live (landing table, per-source tables), "
            "from DFE_CLICKHOUSE_DATA_DATABASE (default `dfe`). Lets the connection "
            "authenticate against one database while DFE tables are qualified against "
            "another -- read it via `effective_data_database`, never directly."
        ),
    )
    hunts_database: str = Field(
        default="dfe_hunts",
        description=(
            "Database holding hunt output (the `detection` table), from "
            "DFE_CLICKHOUSE_HUNTS_DATABASE (default `dfe_hunts`). Separate from the "
            "data database so the hunt-tier ClickHouse roles can be granted on it "
            "alone -- a role granted on the data database would also see every "
            "landing row."
        ),
    )
    otel_database: str = Field(
        default="dfe",
        description=(
            "Database holding the OTel telemetry tables (otel_logs, otel_metrics_*, "
            "otel_traces), from DFE_CLICKHOUSE_OTEL_DATABASE (default `dfe`). The "
            "otel_reader service role's SELECT grant targets this database; mirrors "
            "keda_shim.otel_database. Moved out of the CH-builtin `default` db so "
            "one grant reaches every telemetry table."
        ),
    )
    landing_table: str = Field(
        default="default",
        description="Catch-all table where un-split source data lands (db.landing_table)",
    )
    default_table_profile: str = Field(
        default="timeseries",
        description="Schema profile the bootstrapped landing table is built from (DFE_DEFAULT_TABLE_PROFILE)",
    )
    bootstrap_tables: bool = Field(
        default=True,
        description="Create the DFE databases, landing table, and hunt detection table on startup",
    )
    secure: bool = Field(default=True)
    verify: bool | None = Field(
        default=None,
        description=(
            "Verify the ClickHouse server certificate. None (default) follows the "
            "SCALO_TLS_VERIFY escape valve - i.e. verify ON unless the whole "
            "environment disables it; DFE_CLICKHOUSE_VERIFY overrides per-CH "
            "(set false for self-signed dev/test infra)."
        ),
    )
    ca_cert: str | None = Field(
        default=None,
        description="PEM CA file trusted to verify the ClickHouse server cert (internal CA); DFE_CLICKHOUSE_CA_CERT.",
    )
    connections_min: int = Field(default=10)
    connections_max: int = Field(default=300)
    # Deployment topology FALLBACK: "single" (standalone CH -> MergeTree DDL) or
    # "replicated" (Replicated/Shared db or Cloud -> ReplicatedMergeTree). Live
    # paths sense the server and ignore this; it only decides when sensing is
    # unavailable (no client) or fails. It cannot express ON CLUSTER - a real
    # multi-node cluster is recognised by sensing alone (see EngineResolver).
    topology: str = Field(default="single")
    # Default MergeTree-family VARIANT for tables that do not pin their own engine.
    # The topology above decides the Replicated/Shared prefix; this decides the
    # family (MergeTree / ReplacingMergeTree / SummingMergeTree / ...). Must be a
    # variant the engine registry permits; may be parameterised
    # (e.g. "ReplacingMergeTree(version)").
    default_engine: str = Field(default="MergeTree")
    cloud: ClickHouseCloudSettings = Field(default_factory=ClickHouseCloudSettings)
    resilience: ClickHouseResilienceSettings = Field(default_factory=ClickHouseResilienceSettings)

    @property
    def effective_data_database(self) -> str:
        """Database to qualify DFE table references with.

        Returns ``data_database`` when set, else the connection ``database``.
        Use this anywhere a query names a DFE table as ``db.table`` (discovery,
        promotion, query-views, loader routing) so the connection's default
        database and the data tables' database can diverge.
        """
        return self.data_database or self.database


class HuntsSettings(BaseModel):
    """Hunt scheduler settings."""

    log_path: str = Field(default="hunt_log_path")
    checkpoint_path: str = Field(default="")
    cron_task_timeout: int = Field(
        default=300, description="Scheduler timeout in seconds (-1 = no timeout)"
    )
    hunt_dir: str = Field(default="", description="Directory containing hunt YAML configs")
    rule_repo_dir: str = Field(default="", description="Directory containing Jinja2 rule templates")
    rules_dir: str = Field(
        default="",
        description="YAML directory for API-managed detection rules (DirectoryConfigStore SSoT)",
    )
    num_threads: int = Field(default=1, description="Number of concurrent hunt threads")
    checkpoint_destination: str = Field(
        default="clickhouse", description="Checkpoint storage: 'clickhouse' or 'file'"
    )
    checkpoint_timestamp_field: str = Field(
        default="timestamp_load", description="Timestamp field for checkpointing"
    )
    jitter_seconds: int = Field(
        default=15, description="Max random jitter in seconds for sub-minute stagger"
    )
    scheduling_mode: str = Field(
        default="adaptive",
        description="'adaptive' (REFRESH AFTER backpressure) or 'cron' (rigid, deprecated)",
    )
    min_interval_seconds: int = Field(
        default=0,
        description=(
            "Min seconds between completion and next start"
            " (adaptive mode). 0 = derive from cron frequency"
        ),
    )
    explain_queries: bool = Field(
        default=False,
        description="Run EXPLAIN PLAN before each hunt query and log the plan",
    )
    max_concurrent_queries: int = Field(
        default=0,
        description="Max concurrent hunt queries across all schedulers (0 = unlimited)",
    )
    resource_limit_read_rows: int = Field(
        default=0, description="Warn when a hunt reads more than this many rows (0 = no limit)"
    )
    resource_limit_read_bytes: int = Field(
        default=0, description="Warn when a hunt reads more bytes than this (0 = no limit)"
    )
    resource_limit_memory_bytes: int = Field(
        default=0, description="Warn when a hunt uses more memory than this (0 = no limit)"
    )
    resource_limit_execution_ms: int = Field(
        default=0, description="Warn when a hunt takes longer than this in ms (0 = no limit)"
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
    default_alert_cooldown: str = Field(
        default="1h",
        description="Default alert cooldown window for grouped alerts",
    )
    default_max_alerts_per_run: int = Field(
        default=0,
        description="Default max alerts per execution (0 = unlimited)",
    )
    default_max_sample_events: int = Field(
        default=10,
        description="Default max _json samples in grouped alert body",
    )


class ArtifactorySettings(BaseModel):
    """Artifactory settings for downloading templates."""

    url: str = Field(default="")
    username: str = Field(default="")
    password: str = Field(default="")
    templates_version: str = Field(default="latest")


class RedpandaCloudSettings(BaseModel):
    """Redpanda Cloud Serverless control-plane config (opt-in; WS-C dfe-engine#99).

    Separate from the SASL data-plane connection (``kafka.bootstrap_servers`` +
    ``kafka.sasl_*``, populated by ``dfe kafka lifecycle up`` once the cluster
    exists). This block is the CONTROL-PLANE OAuth2 client (``auth.prd.cloud.
    redpanda.com`` + ``api.redpanda.com``) used to create/delete the cluster and
    mint/revoke the SCRAM-512 data-plane user - the first (proven) provider behind
    ``dfe_engine.kafka.cloud.base.ManagedKafkaProvider``. See
    docs/deployment/managed-kafka-lifecycle.md.
    """

    client_id: str = Field(default="", description="OAuth2 service-account client id.")
    client_secret: str = Field(default="", description="OAuth2 service-account client secret.")
    auth_url: str = Field(
        default="https://auth.prd.cloud.redpanda.com/oauth/token",
        description="OAuth2 client-credentials token endpoint.",
    )
    audience: str = Field(
        default="cloudv2-production.redpanda.cloud", description="OAuth2 token audience."
    )
    api_base: str = Field(default="https://api.redpanda.com", description="Control-plane API base.")
    resource_group: str = Field(
        default="dfe", description="Resource group name (created if absent, find-or-create)."
    )
    cluster_name: str = Field(
        default="dfe-kafka", description="Serverless cluster name (find-or-create, idempotent)."
    )
    kafka_user: str = Field(
        default="dfe-engine", description="SCRAM-512 data-plane username minted on `up`."
    )

    @property
    def configured(self) -> bool:
        """True when the control-plane creds are present (lifecycle usable)."""
        return bool(self.client_id and self.client_secret)


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
            "contract (strimzi/redpanda/msk/redpanda-cloud -> SCRAM-SHA-512; "
            "confluent-cloud -> PLAIN; plaintext -> no auth; msk_iam -> quarantined "
            "IAM path). Never hand-set the mechanism."
        ),
    )
    bootstrap_servers: str = Field(default="localhost:9092")
    security_protocol: str = Field(default="PLAINTEXT")
    sasl_mechanism: str = Field(
        default="",
        description="DERIVED from provider (contract); do not hand-set.",
    )
    sasl_username: str = Field(default="", description="SASL username")
    sasl_password: str = Field(default="", description="SASL password")
    redpanda_cloud: RedpandaCloudSettings = Field(default_factory=RedpandaCloudSettings)

    @model_validator(mode="after")
    def _derive_from_provider(self) -> "KafkaSettings":
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
    """
    Storage backend settings for artifacts and templates.

    Supports local filesystem, S3, and HTTP backends.
    The 'type' field is auto-detected from 'path' if not explicitly set:
    - Paths starting with 's3://' -> S3 backend
    - Paths starting with 'http://' or 'https://' -> HTTP backend
    - All other paths -> Local filesystem backend
    """

    type: str = Field(default="auto", description="Storage type: auto, local, s3, http")
    path: str = Field(default="./artifacts", description="Storage path/URL")
    s3_bucket: str = Field(default="", description="S3 bucket name (for s3 type)")
    s3_region: str = Field(default="", description="AWS region (for s3 type)")


class QuerySettings(BaseModel):
    """Query registry settings.

    Environment variables:
    - DFE_QUERY_YAML_DIR -> query.yaml_dir
    """

    yaml_dir: str = Field(default="", description="YAML directory for query definitions (SSoT)")


class QueryViewSettings(BaseModel):
    """Settings for ClickHouse parameterized view execution.

    Controls the restricted RBAC user, resource limits, and catalog caching.

    Environment variables:
    - DFE_QUERY_VIEWS_RESTRICTED_USER -> query_views.restricted_user
    - DFE_QUERY_VIEWS_RESTRICTED_PASSWORD -> query_views.restricted_password
    - DFE_QUERY_VIEWS_MAX_EXECUTION_TIME -> query_views.max_execution_time
    - DFE_QUERY_VIEWS_MAX_ROWS_TO_READ -> query_views.max_rows_to_read
    - DFE_QUERY_VIEWS_MAX_MEMORY_USAGE -> query_views.max_memory_usage
    """

    restricted_user: str = Field(
        # The query_reader service user minted by governance.ch.ChRbacReconciler;
        # its password comes from the secrets seam (ch/service/query_reader),
        # injected via DFE_QUERY_VIEWS_RESTRICTED_PASSWORD.
        default="dfe_query_reader",
        description="Username for the restricted query user (the query_reader service user)",
    )
    restricted_password: str = Field(default="", description="Password for restricted query user")
    auto_bootstrap: bool = Field(
        default=True,
        description="Automatically apply builtin views on startup (reader RBAC is reconciled separately)",
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
    max_memory_usage: str = Field(
        default="2G",
        description="ClickHouse settings profile max_memory_usage",
    )


class SamplerSettings(BaseModel):
    """Source-sampling settings (the /sources/{source}/sample API + `dfe sample`).

    Two families of mode: cheap "recent"/"random" reads that run inline, and the
    memory-hungry logreducer modes ("smart"/"anomaly") that are gated. logreducer
    is memory-hungry, so its concurrency is capped at ``max_concurrent`` instances,
    each bounded to ``max_memory_gb`` - both small by default.

    Environment variables (DFE_SAMPLER_ prefix):
    - DFE_SAMPLER_DEFAULT_MODE -> sampler.default_mode
    - DFE_SAMPLER_DEFAULT_LIMIT -> sampler.default_limit
    - DFE_SAMPLER_MAX_LIMIT -> sampler.max_limit
    - DFE_SAMPLER_LEVEL -> sampler.level
    - DFE_SAMPLER_MAX_CONCURRENT -> sampler.max_concurrent
    - DFE_SAMPLER_MAX_MEMORY_GB -> sampler.max_memory_gb
    - DFE_SAMPLER_MAX_SCAN_ROWS -> sampler.max_scan_rows
    - DFE_SAMPLER_KAFKA_MAX_MESSAGES -> sampler.kafka_max_messages
    - DFE_SAMPLER_TIMESTAMP_FIELD -> sampler.timestamp_field
    - DFE_SAMPLER_WAIT_SECONDS -> sampler.wait_seconds
    - DFE_SAMPLER_MAX_EXECUTION_TIME -> sampler.max_execution_time
    """

    default_mode: str = Field(
        default="smart",
        description="Default sample mode: recent | random | smart | anomaly",
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
        default="timestamp_load",
        description="Column used to order 'recent' samples (DESC)",
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

    Environment variables (DFE_SYNTHETIC_DATA_ prefix):
    - DFE_SYNTHETIC_DATA_MAX_COUNT -> synthetic_data.max_count
    - DFE_SYNTHETIC_DATA_MAX_RATE_EPS -> synthetic_data.max_rate_eps
    - DFE_SYNTHETIC_DATA_MAX_STREAM_SECONDS -> synthetic_data.max_stream_seconds
    - DFE_SYNTHETIC_DATA_DEFAULT_RATE_EPS -> synthetic_data.default_rate_eps
    - DFE_SYNTHETIC_DATA_AUTOSTART_SCHEMAS -> synthetic_data.autostart_schemas
    - DFE_SYNTHETIC_DATA_AUTOSTART_RECEIVER_URL -> synthetic_data.autostart_receiver_url
    - DFE_SYNTHETIC_DATA_AUTOSTART_RATE_EPS -> synthetic_data.autostart_rate_eps
    - DFE_SYNTHETIC_DATA_AUTOSTART_SEED -> synthetic_data.autostart_seed
    - DFE_SYNTHETIC_DATA_MAX_CONCURRENT_STREAMS -> synthetic_data.max_concurrent_streams
    - DFE_SYNTHETIC_DATA_ALLOWED_RECEIVER_HOSTS -> synthetic_data.allowed_receiver_hosts
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

    Environment variables (DFE_KEDA_SHIM_ prefix):
    - DFE_KEDA_SHIM_HOST -> keda_shim.host
    - DFE_KEDA_SHIM_PORT -> keda_shim.port
    - DFE_KEDA_SHIM_QUERY_CONFIG -> keda_shim.query_config (override catalogue path)
    - DFE_KEDA_SHIM_OTEL_DATABASE -> keda_shim.otel_database (HyperDX otel CH db)
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
    otel_database: str = Field(
        default="dfe",
        description="ClickHouse database holding HyperDX's otel_metrics_gauge (pressure source)",
    )


class RepositorySettings(BaseModel):
    """Repository (scope-aligned small-object store) settings.

    Environment variables:
    - DFE_REPOSITORY_DATABASE -> repository.database
    - DFE_REPOSITORY_MAX_PREFS_BYTES -> repository.max_prefs_bytes
    - DFE_REPOSITORY_MAX_OBJECT_BYTES -> repository.max_object_bytes
    """

    database: str = Field(
        default="dfe_internal",
        description="ClickHouse database for the repository table (engine-only, hidden "
        "from HyperDX per-group users)",
    )
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


class DeploymentSettings(BaseModel):
    """Deployment configuration settings.

    Environment variables:
    - DFE_DEPLOYMENT_CONFIG_DIR -> deployment.config_dir
    """

    config_dir: str = Field(default="", description="YAML directory for deployment configurations")


class SchemasSettings(BaseModel):
    """Shared schemas settings (dfe-schemas submodule).

    Environment variables:
    - DFE_SCHEMAS_DIR -> schemas.schemas_dir
    """

    schemas_dir: str = Field(
        default="",
        description="Root of dfe-schemas directory (submodule or standalone checkout)",
    )


class SourceSettings(BaseModel):
    """Source registry settings.

    Environment variables:
    - DFE_SOURCES_DIR -> source.sources_dir
    - DFE_SOURCE_BUILDS_DIR -> source.builds_dir
    - DFE_SOURCE_PLANS_DIR -> source.plans_dir
    - DFE_SOURCE_DEPLOYS_DIR -> source.deploys_dir
    """

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


class FieldMapSettings(BaseModel):
    """Field map registry settings.

    Environment variables:
    - DFE_FIELDMAPS_DIR -> fieldmap.fieldmaps_dir
    """

    fieldmaps_dir: str = Field(
        default="", description="YAML directory for FieldMap definitions (SSoT)"
    )


class ServicesSettings(BaseModel):
    """Endpoints for managed DFE services (the transform-wasm compile/test proxy +
    the Rust-services YAML config replica dir). The per-service health/metrics URLs
    were dropped with services/state.py -- runtime state is the surfaces registry now.

    Environment variables:
    - DFE_SERVICES_TRANSFORM_WASM_URL -> services.transform_wasm_url
    - DFE_SERVICES_TRANSFORM_WASM_COMPILER_URL -> services.transform_wasm_compiler_url
    - DFE_SERVICES_CONFIG_YAML_DIR -> services.config_yaml_dir
    """

    transform_wasm_url: str = Field(default="http://localhost:8080")
    transform_wasm_compiler_url: str = Field(default="http://localhost:8090")
    config_yaml_dir: str = Field(
        default="", description="YAML config replica directory for Rust services"
    )


class HelmSettings(BaseModel):
    """Helm values compiler settings.

    Environment variables:
    - DFE_HELM_OUTPUT_DIR -> helm.output_dir
    - DFE_HELM_ENVIRONMENT_FILE -> helm.environment_file
    """

    output_dir: str = Field(default="", description="Output directory for compiled Helm values")
    environment_file: str = Field(default="", description="Path to environment config YAML")


class OIDCSettings(BaseModel):
    """OIDC provider settings.

    Environment variables:
    - DFE_AUTH_OIDC_PROVIDERS_DIR -> auth.oidc.providers_dir
    - DFE_AUTH_OIDC_SYNC_ENABLED -> auth.oidc.sync_enabled
    - DFE_AUTH_OIDC_SYNC_ON_STARTUP -> auth.oidc.sync_on_startup
    """

    providers_dir: str = Field(default="", description="OIDC provider config directory")
    sync_enabled: bool = Field(default=True, description="Enable background group sync")
    sync_on_startup: bool = Field(default=True, description="Sync on startup")


class SeedAccount(BaseModel):
    """One named local account seeded + RECONCILED on every bootstrap.

    Unlike the break-glass admin (seeded only into an empty store), each seed
    account is reconciled on every startup: the configured password and groups
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


class LocalAuthSettings(BaseModel):
    """Built-in local-account bootstrap config (nested under auth.local).

    When enabled, the engine seeds admin/operator/viewer accounts at startup with
    these passwords. Populated by load_settings from the DFE_AUTH_LOCAL_* env
    vars; field names must match the keys set there.
    """

    enabled: bool = Field(default=False, description="Seed built-in local accounts")
    org_id: str = Field(default="default", description="Org id for the seeded accounts")
    admin_name: str = Field(default="", description="Bootstrap admin username")
    admin_password: str = Field(default="", description="Bootstrap admin password")
    operator_password: str = Field(default="", description="Bootstrap operator password")
    viewer_password: str = Field(default="", description="Bootstrap viewer password")
    seed_accounts: list[SeedAccount] = Field(
        default_factory=list,
        description=(
            "Named accounts reconciled on EVERY startup (config wins), so shared team "
            "logins survive a teardown+rebuild unchanged (dfe-infra #106). Set from "
            "DFE_AUTH_LOCAL_SEED_ACCOUNTS: a JSON list of "
            "{username, password, groups}. Profile-independent (same across slim/single/scale)."
        ),
    )


class AccountStoreSettings(BaseModel):
    """Document-store / mongo-wire connection for the local account store.

    Used when ``auth.store_backend`` resolves to document. The URI carries the
    SCRAM credential (sourced from the secret manager at deploy time); the
    database is separate from HyperDX's for isolation.

    Environment variables:
    - DFE_AUTH_ACCOUNTS_STORE_URI -> auth.accounts_store.uri
    - DFE_AUTH_ACCOUNTS_STORE_DATABASE -> auth.accounts_store.database
    - DFE_AUTH_ACCOUNTS_STORE_COLLECTION -> auth.accounts_store.collection
    """

    uri: str = Field(
        default="",
        description="mongodb:// connection URI (SCRAM credential from the secret manager)",
    )
    database: str = Field(
        default="dfe_engine",
        description="Document database name (separate from HyperDX's)",
    )
    collection: str = Field(default="accounts", description="Accounts collection name")


class AuthSettings(BaseModel):
    """Authorization settings.

    Bespoke role→permission RBAC. Zero external dependencies.

    Environment variables:
    - DFE_AUTH_ENABLED -> auth.enabled
    - DFE_AUTH_DIR -> auth.auth_dir
    """

    enabled: bool = Field(
        default=True,
        description=(
            "Enable authorization. Defaults ON so it matches env's default "
            "'production' posture -- with it off, api/deps.py hands an "
            "unauthenticated request the admin role. Dev and test opt out with "
            "DFE_AUTH_ENABLED=false, which needs DFE_ENV set to a dev posture too "
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
            "Trust X-Oidc-* identity headers (auth Path 1). Enable ONLY when a "
            "trusted proxy (e.g. Envoy Gateway) authenticates the user and "
            "injects these headers AND the engine is reachable only via that "
            "proxy. Default off = fail closed: standalone/unfronted deployments "
            "ignore these client-spoofable headers."
        ),
    )
    oidc: OIDCSettings = Field(default_factory=OIDCSettings)
    local: LocalAuthSettings = Field(default_factory=LocalAuthSettings)
    store_backend: str = Field(
        default="auto",
        description=(
            "Central backend for BOTH local users and groups (config-cascade "
            "overridable): 'auto' (default -- document when a reachable URI is "
            "configured, else yaml files), 'document' (force; fail if unreachable), "
            "or 'yaml'. gitcrud is not a store here -- only the break-glass admin is "
            "additionally git-persisted, as the teardown backstop."
        ),
    )
    accounts_store: AccountStoreSettings = Field(default_factory=AccountStoreSettings)


class HyperDXSettings(BaseModel):
    """HyperDX integration settings.

    Environment variables:
    - DFE_HYPERDX_BASE_URL -> hyperdx.base_url
    - DFE_HYPERDX_ENABLED -> hyperdx.enabled
    - DFE_HYPERDX_API_KEY_ENV -> hyperdx.api_key_env
    """

    base_url: str = Field(default="", description="HyperDX API base URL")
    api_key_env: str = Field(
        default="DFE_HYPERDX_API_KEY",
        description="Env var for HyperDX API key",
    )
    enabled: bool = Field(default=False, description="Enable HyperDX integration")


class GitopsSettings(BaseModel):
    """Deploy-specific gitops repo the engine renders artifacts into.

    Disabled by default. When enabled, the engine clones/pulls repo_url (or uses
    an existing local_path), writes rendered artifacts, commits only on change,
    and pushes when push=True. Secrets never go here -- only declarative config
    Argo consumes.

    Environment variables (DFE_GITOPS_ prefix):
    - DFE_GITOPS_ENABLED -> gitops.enabled
    - DFE_GITOPS_REPO_URL -> gitops.repo_url
    - DFE_GITOPS_BRANCH -> gitops.branch
    - DFE_GITOPS_LOCAL_PATH -> gitops.local_path
    - DFE_GITOPS_PUSH -> gitops.push
    - DFE_GITOPS_USERNAME / DFE_GITOPS_TOKEN -> HTTPS push auth
    - DFE_GITOPS_AUTHOR_NAME / DFE_GITOPS_AUTHOR_EMAIL -> commit identity
    - DFE_GITOPS_MODE -> gitops.mode
    - DFE_GITOPS_FORGE_PROVIDER -> gitops.forge_provider (review-PR seam)
    - DFE_GITOPS_FORGE_API_BASE -> gitops.forge_api_base
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
            "production DFE_ENV; 'team' (default) refuses auto-merge outside "
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


# Known placeholder JWT secret - fine for local dev, REJECTED in a production
# posture when auth is on (see DFESettings._reject_insecure_production_posture).
_DEV_JWT_SECRET = "dev-secret-key-change-in-production"

# Postures that are NOT production; anything else (incl. the default
# "production") is treated as production for the placeholder-secret guard.
_NON_PROD_ENVS = frozenset({"dev", "development", "local", "test", "ci"})


def is_dev_posture(env: str) -> bool:
    """True when DFE_ENV declares a non-production posture (dev/local/test/ci)."""
    return env.strip().lower() in _NON_PROD_ENVS


class APISettings(BaseModel):
    """API server settings.

    Environment variables:
    - DFE_API_HOST -> api.host
    - DFE_API_PORT -> api.port
    - DFE_API_JWT_SECRET -> api.jwt_secret
    - DFE_API_CORS_ORIGINS -> api.cors_origins (comma-separated)
    - DFE_API_JWT_EXPIRE_MINUTES -> api.jwt_expire_minutes
    - DFE_API_ELASTIC_CONVERTER_MAX_UPLOAD_BYTES -> api.elastic_converter_max_upload_bytes
    - DFE_API_ELASTIC_CONVERTER_READ_CHUNK_SIZE -> api.elastic_converter_read_chunk_size
    - DFE_API_ELASTIC_CONVERTER_CONTENT_LENGTH_SLACK_BYTES ->
      api.elastic_converter_content_length_slack_bytes
    """

    host: str = Field(default="0.0.0.0", description="API server bind address")  # noqa: S104
    port: int = Field(default=8000, description="API server port")
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://localhost:5174",
            "http://localhost:3000",
        ],
        description="CORS allowed origins",
    )
    jwt_secret: str = Field(
        default=_DEV_JWT_SECRET,
        description=(
            "Legacy HS256 secret - UNUSED. The JWT authority signs ES384 with an "
            "asymmetric key held in scalo.secrets; retained only for config compat."
        ),
    )
    jwt_algorithm: str = Field(
        default="ES384",
        description="JWT signing algorithm - ES384 (ECDSA P-384 + SHA-384, CNSA-aligned). Crypto-agile.",
    )
    jwt_expire_minutes: int = Field(default=60, description="JWT token expiry in minutes")
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
                "set a strong DFE_API_JWT_SECRET"
            )
        return v


class SecretsSettings(BaseModel):
    """Backing-service seam for secrets the engine MINTS - backend by config.

    The engine reads runtime config secrets from env; this is the WRITE seam for
    secrets it generates (per-group ClickHouse passwords, OIDC client secrets, API
    keys). ``provider`` selects the scalo.secrets backend and NEVER a hardcoded
    product: 'file'/'ansible_vault' for dfe-docker (a local encrypted file, no extra
    service), 'openbao' on k8s (external-first), 'aws'/'gcp'/'azure' for cloud
    (deferred). See docs/deployment/backing-services.md.

    Environment variables (DFE_SECRETS_ prefix):
    - DFE_SECRETS_PROVIDER -> secrets.provider (file|ansible_vault|openbao|aws|gcp|azure)
    - DFE_SECRETS_PATH -> secrets.path (file/ansible_vault root)
    - DFE_SECRETS_ADDR -> secrets.addr (openbao/vault address)
    - DFE_SECRETS_MOUNT -> secrets.mount (kv mount / key prefix)
    - DFE_SECRETS_ROLE -> secrets.role (openbao AppRole role id)
    """

    provider: str = Field(default="file", description="scalo.secrets backend provider")
    path: str = Field(default="./.secrets", description="file/ansible_vault root path")
    addr: str = Field(default="", description="openbao/vault address")
    mount: str = Field(default="dfe", description="kv mount / key prefix")
    role: str = Field(default="", description="openbao AppRole role id")


class DFESettings(BaseModel):
    """Main DFE Engine settings container."""

    config_dir: str = Field(
        default="",
        description="Root config directory (dfe-devex submodule). Auto-resolves registry subdirs.",
    )
    clickhouse: ClickHouseSettings = Field(default_factory=ClickHouseSettings)
    hunts: HuntsSettings = Field(default_factory=HuntsSettings)
    artifactory: ArtifactorySettings = Field(default_factory=ArtifactorySettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    query: QuerySettings = Field(default_factory=QuerySettings)
    query_views: QueryViewSettings = Field(default_factory=QueryViewSettings)
    sampler: SamplerSettings = Field(default_factory=SamplerSettings)
    synthetic_data: SyntheticDataSettings = Field(default_factory=SyntheticDataSettings)
    keda_shim: KedaShimSettings = Field(default_factory=KedaShimSettings)
    schemas: SchemasSettings = Field(default_factory=SchemasSettings)
    source: SourceSettings = Field(default_factory=SourceSettings)
    fieldmap: FieldMapSettings = Field(default_factory=FieldMapSettings)
    services: ServicesSettings = Field(default_factory=ServicesSettings)
    repository: RepositorySettings = Field(default_factory=RepositorySettings)
    deployment: DeploymentSettings = Field(default_factory=DeploymentSettings)
    helm: HelmSettings = Field(default_factory=HelmSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    hyperdx: HyperDXSettings = Field(default_factory=HyperDXSettings)
    gitops: GitopsSettings = Field(default_factory=GitopsSettings)
    api: APISettings = Field(default_factory=APISettings)
    secrets: SecretsSettings = Field(default_factory=SecretsSettings)
    env: str = Field(
        default="production",
        description=(
            "Deployment posture. 'production' (default, secure) rejects the "
            "placeholder api.jwt_secret when auth is enabled; set DFE_ENV to "
            "dev/development/local/test/ci for local development. DFE_ENV."
        ),
    )

    @model_validator(mode="after")
    def _reject_insecure_production_posture(self) -> "DFESettings":
        # Two ways a production posture can enforce nothing. Both are errors here
        # rather than warnings, because a warning leaves the process running.
        is_prod = not is_dev_posture(self.env)
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
                "unauthenticated caller the admin role. Set DFE_AUTH_ENABLED=true, "
                "or DFE_ENV=dev for local development"
            )

        # A production deployment that turns auth on but never overrode the known
        # dev jwt_secret lets anyone forge tokens. Minimum length is enforced on
        # the field itself.
        if self.api.jwt_secret == _DEV_JWT_SECRET:
            raise ValueError(
                "api.jwt_secret is the known dev placeholder but env is "
                f"'{self.env}' with auth enabled; set a strong DFE_API_JWT_SECRET "
                "(or DFE_ENV=dev for local development)"
            )
        return self


def _load_defaults() -> dict:
    """Load default configuration from defaults.yaml."""
    defaults_path = Path(__file__).parent / "defaults.yaml"
    if defaults_path.exists():
        return yaml_load(defaults_path) or {}
    return {}


def _get_env_overrides() -> dict:
    """Get configuration overrides from environment variables.

    DFE_ prefixed vars take precedence over legacy names.
    """
    # Mixed value types by design: section dicts plus top-level scalars
    # (env, config_dir), so the annotation is Any, not dict.
    overrides: dict[str, Any] = {
        "clickhouse": {},
        "hunts": {},
        "artifactory": {},
        "kafka": {},
        "storage": {},
        "query": {},
        "query_views": {},
        "sampler": {},
        "synthetic_data": {},
        "keda_shim": {},
        "schemas": {},
        "source": {},
        "fieldmap": {},
        "services": {},
        "repository": {},
        "deployment": {},
        "helm": {},
        "auth": {},
        "hyperdx": {},
        "gitops": {},
        "api": {},
        "secrets": {},
    }

    # ClickHouse settings (DFE_ prefix with legacy fallbacks)
    if val := _get_env("DFE_CLICKHOUSE_HOST", "CLICKHOUSE_HOST"):
        overrides["clickhouse"]["host"] = val
    if val := _get_env("DFE_CLICKHOUSE_PORT", "CLICKHOUSE_PORT"):
        overrides["clickhouse"]["port"] = int(val)
    if val := _get_env("DFE_CLICKHOUSE_USERNAME", "CLICKHOUSE_USER"):
        overrides["clickhouse"]["username"] = val
    if val := _get_env("DFE_CLICKHOUSE_PASSWORD", "CLICKHOUSE_PASSWORD"):
        overrides["clickhouse"]["password"] = val
    if val := _get_env("DFE_CLICKHOUSE_DATABASE", "CLICKHOUSE_DATABASE"):
        overrides["clickhouse"]["database"] = val
    if val := _get_env("DFE_CLICKHOUSE_DATA_DATABASE", "CLICKHOUSE_DATA_DATABASE"):
        overrides["clickhouse"]["data_database"] = val
    if val := _get_env("DFE_CLICKHOUSE_OTEL_DATABASE", "CLICKHOUSE_OTEL_DATABASE"):
        overrides["clickhouse"]["otel_database"] = val
    if val := _get_env("DFE_CLICKHOUSE_LANDING_TABLE", "CLICKHOUSE_LANDING_TABLE"):
        overrides["clickhouse"]["landing_table"] = val
    if val := _get_env("DFE_DEFAULT_TABLE_PROFILE"):
        overrides["clickhouse"]["default_table_profile"] = val
    if val := _get_env("DFE_CLICKHOUSE_BOOTSTRAP_TABLES"):
        overrides["clickhouse"]["bootstrap_tables"] = val.lower() in ("true", "1", "yes")
    if val := _get_env("DFE_CLICKHOUSE_SECURE", "CLICKHOUSE_SECURE"):
        overrides["clickhouse"]["secure"] = val.lower() in ("true", "1", "yes")
    if val := _get_env("DFE_CLICKHOUSE_VERIFY", "CLICKHOUSE_VERIFY"):
        overrides["clickhouse"]["verify"] = val.lower() in ("true", "1", "yes")
    if val := _get_env("DFE_CLICKHOUSE_CA_CERT", "CLICKHOUSE_CA_CERT"):
        overrides["clickhouse"]["ca_cert"] = val

    # Engine-wide TLS escape valves (config cascade): map the DFE_-prefixed knobs
    # onto scalo's env seam so ONE setting relaxes every scalo-minted client - CH,
    # scalo.http (HyperDX/OIDC), scalo.secrets (OpenBao) - consistently. Both are
    # secure-by-default; setdefault lets an explicit SCALO_* env win.
    #   DFE_TLS_VERIFY=false    -> drop cert verification (self-signed dev/test).
    #   DFE_TLS_ALLOW_WEAK=true -> accept a legacy peer below the algorithm floor.
    if val := _get_env("DFE_TLS_VERIFY"):
        os.environ.setdefault("SCALO_TLS_VERIFY", val)
    if val := _get_env("DFE_TLS_ALLOW_WEAK"):
        os.environ.setdefault("SCALO_TLS_ALLOW_WEAK", val)
    if val := _get_env("DFE_CLICKHOUSE_CONNECTIONS_MIN"):
        overrides["clickhouse"]["connections_min"] = int(val)
    if val := _get_env("DFE_CLICKHOUSE_CONNECTIONS_MAX"):
        overrides["clickhouse"]["connections_max"] = int(val)
    if val := _get_env("DFE_CLICKHOUSE_TOPOLOGY"):
        overrides["clickhouse"]["topology"] = val

    # ClickHouse Cloud lifecycle (control-plane mgmt API; opt-in, billable).
    ch_cloud: dict = {}
    if val := _get_env("DFE_CLICKHOUSE_CLOUD_API_KEY_ID"):
        ch_cloud["api_key_id"] = val
    if val := _get_env("DFE_CLICKHOUSE_CLOUD_API_KEY_SECRET"):
        ch_cloud["api_key_secret"] = val
    if val := _get_env("DFE_CLICKHOUSE_CLOUD_API_BASE"):
        ch_cloud["api_base"] = val
    if val := _get_env("DFE_CLICKHOUSE_CLOUD_ORGANIZATION_ID"):
        ch_cloud["organization_id"] = val
    if val := _get_env("DFE_CLICKHOUSE_CLOUD_SERVICE_ID"):
        ch_cloud["service_id"] = val
    if val := _get_env("DFE_CLICKHOUSE_CLOUD_SERVICE", "DFE_CLICKHOUSE_CLOUD_SERVICE_NAME"):
        ch_cloud["service_name"] = val
    if val := _get_env("DFE_CLICKHOUSE_CLOUD_AUTOWAKE"):
        ch_cloud["autowake"] = val.lower() in ("true", "1", "yes")
    if ch_cloud:
        overrides["clickhouse"]["cloud"] = ch_cloud

    # Hunts settings
    if val := _get_env("DFE_HUNT_LOG_PATH", "HUNT_LOG_PATH"):
        overrides["hunts"]["log_path"] = val
    if val := _get_env("DFE_HUNTS_DIR"):
        overrides["hunts"]["hunt_dir"] = val
    if val := _get_env("DFE_HUNTS_RULE_REPO_DIR"):
        overrides["hunts"]["rule_repo_dir"] = val
    if val := _get_env("DFE_HUNTS_RULES_DIR"):
        overrides["hunts"]["rules_dir"] = val
    if val := _get_env("DFE_HUNTS_NUM_THREADS"):
        overrides["hunts"]["num_threads"] = int(val)
    if val := _get_env("DFE_HUNTS_CHECKPOINT_DESTINATION"):
        overrides["hunts"]["checkpoint_destination"] = val
    if val := _get_env("DFE_HUNTS_CHECKPOINT_TIMESTAMP_FIELD"):
        overrides["hunts"]["checkpoint_timestamp_field"] = val
    if val := _get_env("DFE_HUNTS_CHECKPOINT_PATH"):
        overrides["hunts"]["checkpoint_path"] = val
    if val := _get_env("DFE_HUNTS_CRON_TASK_TIMEOUT"):
        overrides["hunts"]["cron_task_timeout"] = int(val)
    if val := _get_env("DFE_HUNTS_JITTER_SECONDS"):
        overrides["hunts"]["jitter_seconds"] = int(val)
    if val := _get_env("DFE_HUNTS_SCHEDULING_MODE"):
        overrides["hunts"]["scheduling_mode"] = val
    if val := _get_env("DFE_HUNTS_MIN_INTERVAL_SECONDS"):
        overrides["hunts"]["min_interval_seconds"] = int(val)
    if val := _get_env("DFE_HUNTS_EXPLAIN_QUERIES"):
        overrides["hunts"]["explain_queries"] = val.lower() in ("true", "1", "yes")
    if val := _get_env("DFE_HUNTS_MAX_CONCURRENT_QUERIES"):
        overrides["hunts"]["max_concurrent_queries"] = int(val)
    if val := _get_env("DFE_HUNTS_RESOURCE_LIMIT_READ_ROWS"):
        overrides["hunts"]["resource_limit_read_rows"] = int(val)
    if val := _get_env("DFE_HUNTS_RESOURCE_LIMIT_READ_BYTES"):
        overrides["hunts"]["resource_limit_read_bytes"] = int(val)
    if val := _get_env("DFE_HUNTS_RESOURCE_LIMIT_MEMORY_BYTES"):
        overrides["hunts"]["resource_limit_memory_bytes"] = int(val)
    if val := _get_env("DFE_HUNTS_RESOURCE_LIMIT_EXECUTION_MS"):
        overrides["hunts"]["resource_limit_execution_ms"] = int(val)
    if val := _get_env("DFE_HUNTS_ALERT_CHANNELS"):
        overrides["hunts"]["alert_channels"] = [u.strip() for u in val.split(",") if u.strip()]
    if val := _get_env("DFE_HUNTS_ALERT_DESTINATIONS"):
        # JSON format: {"slack-dfe-alerts": "slack://T.../B.../x.../"}
        import json

        try:
            overrides["hunts"]["alert_destinations"] = json.loads(val)
        except json.JSONDecodeError:
            pass
    if val := _get_env("DFE_HUNTS_ALERT_DESTINATIONS_DIR"):
        overrides["hunts"]["alert_destinations_dir"] = val
    if val := _get_env("DFE_HUNTS_DEFAULT_ALERT_COOLDOWN"):
        overrides["hunts"]["default_alert_cooldown"] = val
    if val := _get_env("DFE_HUNTS_DEFAULT_MAX_ALERTS_PER_RUN"):
        overrides["hunts"]["default_max_alerts_per_run"] = int(val)
    if val := _get_env("DFE_HUNTS_DEFAULT_MAX_SAMPLE_EVENTS"):
        overrides["hunts"]["default_max_sample_events"] = int(val)

    # Artifactory settings
    if val := _get_env("DFE_ARTIFACTORY_URL", "ARTIFACTORY_VECTOR_TEMPLATES"):
        overrides["artifactory"]["url"] = val
    if val := _get_env("DFE_ARTIFACTORY_USERNAME", "ARTIFACTORY_USERNAME"):
        overrides["artifactory"]["username"] = val
    if val := _get_env("DFE_ARTIFACTORY_PASSWORD", "ARTIFACTORY_PASSWORD"):
        overrides["artifactory"]["password"] = val
    if val := _get_env("DFE_TEMPLATES_VERSION", "TEMPLATES_VERSION"):
        overrides["artifactory"]["templates_version"] = val

    # Kafka settings (DFE_ prefix with legacy fallbacks)
    # DFE_KAFKA_PROVIDER DERIVES protocol + mechanism (contract dfe-engine#98);
    # deploys set it, not the mechanism. security_protocol/sasl_mechanism below
    # are the transition/manual path (empty provider) - see KafkaSettings.
    if val := _get_env("DFE_KAFKA_PROVIDER"):
        overrides["kafka"]["provider"] = val
    if val := _get_env("DFE_KAFKA_BOOTSTRAP_SERVERS", "KAFKA_BOOTSTRAP_SERVERS"):
        overrides["kafka"]["bootstrap_servers"] = val
    if val := _get_env("DFE_KAFKA_SECURITY_PROTOCOL", "KAFKA_SECURITY_PROTOCOL"):
        overrides["kafka"]["security_protocol"] = val
    if val := _get_env("DFE_KAFKA_SASL_MECHANISM", "KAFKA_SASL_MECHANISM"):
        overrides["kafka"]["sasl_mechanism"] = val
    if val := _get_env("DFE_KAFKA_SASL_USERNAME", "KAFKA_SASL_USERNAME"):
        overrides["kafka"]["sasl_username"] = val
    if val := _get_env("DFE_KAFKA_SASL_PASSWORD", "KAFKA_SASL_PASSWORD"):
        overrides["kafka"]["sasl_password"] = val

    # Redpanda Cloud lifecycle (control-plane OAuth2 client; opt-in, WS-C
    # dfe-engine#99). DFE_REDPANDA_API_KEY/_SECRET are the pre-existing names this
    # repo's .env already uses for the OAuth2 client id/secret - kept as the
    # primary names rather than inventing a DFE_REDPANDA_CLOUD_CLIENT_* pair.
    redpanda_cloud: dict = {}
    if val := _get_env("DFE_REDPANDA_API_KEY", "DFE_REDPANDA_CLOUD_CLIENT_ID"):
        redpanda_cloud["client_id"] = val
    if val := _get_env("DFE_REDPANDA_API_SECRET", "DFE_REDPANDA_CLOUD_CLIENT_SECRET"):
        redpanda_cloud["client_secret"] = val
    if val := _get_env("DFE_REDPANDA_CLOUD_AUTH_URL"):
        redpanda_cloud["auth_url"] = val
    if val := _get_env("DFE_REDPANDA_CLOUD_AUDIENCE"):
        redpanda_cloud["audience"] = val
    if val := _get_env("DFE_REDPANDA_CLOUD_API_BASE"):
        redpanda_cloud["api_base"] = val
    if val := _get_env("DFE_REDPANDA_CLOUD_RESOURCE_GROUP"):
        redpanda_cloud["resource_group"] = val
    if val := _get_env("DFE_REDPANDA_CLOUD_CLUSTER_NAME"):
        redpanda_cloud["cluster_name"] = val
    if val := _get_env("DFE_REDPANDA_CLOUD_KAFKA_USER"):
        redpanda_cloud["kafka_user"] = val
    if redpanda_cloud:
        overrides["kafka"]["redpanda_cloud"] = redpanda_cloud

    # Storage settings (for on-prem/Rancher deployments)
    if val := _get_env("DFE_STORAGE_TYPE"):
        overrides["storage"]["type"] = val
    if val := _get_env("DFE_STORAGE_PATH"):
        overrides["storage"]["path"] = val
    if val := _get_env("DFE_S3_BUCKET"):
        overrides["storage"]["s3_bucket"] = val
    if val := _get_env("DFE_S3_REGION"):
        overrides["storage"]["s3_region"] = val

    # Query settings
    if val := _get_env("DFE_QUERY_YAML_DIR"):
        overrides["query"]["yaml_dir"] = val

    # Query views settings (parameterized views)
    if val := _get_env("DFE_QUERY_VIEWS_RESTRICTED_USER"):
        overrides["query_views"]["restricted_user"] = val
    if val := _get_env("DFE_QUERY_VIEWS_RESTRICTED_PASSWORD"):
        overrides["query_views"]["restricted_password"] = val
    if val := _get_env("DFE_QUERY_VIEWS_MAX_EXECUTION_TIME"):
        overrides["query_views"]["max_execution_time"] = int(val)
    if val := _get_env("DFE_QUERY_VIEWS_MAX_ROWS_TO_READ"):
        overrides["query_views"]["max_rows_to_read"] = int(val)
    if val := _get_env("DFE_QUERY_VIEWS_MAX_MEMORY_USAGE"):
        overrides["query_views"]["max_memory_usage"] = val

    # Sampler settings (source sampling: recent/random/smart/anomaly)
    if val := _get_env("DFE_SAMPLER_DEFAULT_MODE"):
        overrides["sampler"]["default_mode"] = val
    if val := _get_env("DFE_SAMPLER_DEFAULT_LIMIT"):
        overrides["sampler"]["default_limit"] = int(val)
    if val := _get_env("DFE_SAMPLER_MAX_LIMIT"):
        overrides["sampler"]["max_limit"] = int(val)
    if val := _get_env("DFE_SAMPLER_LEVEL"):
        overrides["sampler"]["level"] = val
    if val := _get_env("DFE_SAMPLER_MAX_CONCURRENT"):
        overrides["sampler"]["max_concurrent"] = int(val)
    if val := _get_env("DFE_SAMPLER_MAX_MEMORY_GB"):
        overrides["sampler"]["max_memory_gb"] = float(val)
    if val := _get_env("DFE_SAMPLER_MAX_SCAN_ROWS"):
        overrides["sampler"]["max_scan_rows"] = int(val)
    if val := _get_env("DFE_SAMPLER_KAFKA_MAX_MESSAGES"):
        overrides["sampler"]["kafka_max_messages"] = int(val)
    if val := _get_env("DFE_SAMPLER_TIMESTAMP_FIELD"):
        overrides["sampler"]["timestamp_field"] = val
    if val := _get_env("DFE_SAMPLER_WAIT_SECONDS"):
        overrides["sampler"]["wait_seconds"] = float(val)
    if val := _get_env("DFE_SAMPLER_MAX_EXECUTION_TIME"):
        overrides["sampler"]["max_execution_time"] = int(val)

    # Synthetic data settings (ceilings + the standing demo stream)
    if val := _get_env("DFE_SYNTHETIC_DATA_MAX_COUNT"):
        overrides["synthetic_data"]["max_count"] = int(val)
    if val := _get_env("DFE_SYNTHETIC_DATA_MAX_RATE_EPS"):
        overrides["synthetic_data"]["max_rate_eps"] = float(val)
    if val := _get_env("DFE_SYNTHETIC_DATA_MAX_STREAM_SECONDS"):
        overrides["synthetic_data"]["max_stream_seconds"] = float(val)
    if val := _get_env("DFE_SYNTHETIC_DATA_DEFAULT_RATE_EPS"):
        overrides["synthetic_data"]["default_rate_eps"] = float(val)
    if val := _get_env("DFE_SYNTHETIC_DATA_AUTOSTART_SCHEMAS"):
        overrides["synthetic_data"]["autostart_schemas"] = val
    if val := _get_env("DFE_SYNTHETIC_DATA_AUTOSTART_RECEIVER_URL"):
        overrides["synthetic_data"]["autostart_receiver_url"] = val
    if val := _get_env("DFE_SYNTHETIC_DATA_AUTOSTART_RATE_EPS"):
        overrides["synthetic_data"]["autostart_rate_eps"] = float(val)
    if val := _get_env("DFE_SYNTHETIC_DATA_AUTOSTART_SEED"):
        overrides["synthetic_data"]["autostart_seed"] = int(val)
    if val := _get_env("DFE_SYNTHETIC_DATA_MAX_CONCURRENT_STREAMS"):
        overrides["synthetic_data"]["max_concurrent_streams"] = int(val)
    if val := _get_env("DFE_SYNTHETIC_DATA_ALLOWED_RECEIVER_HOSTS"):
        overrides["synthetic_data"]["allowed_receiver_hosts"] = val

    # KEDA shim settings (the metrics-api -> ClickHouse query adapter)
    if val := _get_env("DFE_KEDA_SHIM_HOST"):
        overrides["keda_shim"]["host"] = val
    if val := _get_env("DFE_KEDA_SHIM_PORT"):
        overrides["keda_shim"]["port"] = int(val)
    if val := _get_env("DFE_KEDA_SHIM_QUERY_CONFIG"):
        overrides["keda_shim"]["query_config"] = val
    if val := _get_env("DFE_KEDA_SHIM_OTEL_DATABASE"):
        overrides["keda_shim"]["otel_database"] = val

    # Schemas settings (dfe-schemas submodule)
    if val := _get_env("DFE_SCHEMAS_DIR"):
        overrides["schemas"]["schemas_dir"] = val

    # Source settings
    if val := _get_env("DFE_SOURCES_DIR"):
        overrides["source"]["sources_dir"] = val
    if val := _get_env("DFE_SOURCE_BUILDS_DIR"):
        overrides["source"]["builds_dir"] = val
    if val := _get_env("DFE_SOURCE_PLANS_DIR"):
        overrides["source"]["plans_dir"] = val
    if val := _get_env("DFE_SOURCE_DEPLOYS_DIR"):
        overrides["source"]["deploys_dir"] = val

    # FieldMap settings
    if val := _get_env("DFE_FIELDMAPS_DIR"):
        overrides["fieldmap"]["fieldmaps_dir"] = val

    # Services settings
    if val := _get_env("DFE_SERVICES_TRANSFORM_WASM_URL"):
        overrides["services"]["transform_wasm_url"] = val
    if val := _get_env("DFE_SERVICES_TRANSFORM_WASM_COMPILER_URL"):
        overrides["services"]["transform_wasm_compiler_url"] = val
    if val := _get_env("DFE_SERVICES_CONFIG_YAML_DIR"):
        overrides["services"]["config_yaml_dir"] = val

    # Repository (small-object store) settings
    if val := _get_env("DFE_REPOSITORY_DATABASE"):
        overrides["repository"]["database"] = val
    if val := _get_env("DFE_REPOSITORY_MAX_PREFS_BYTES"):
        overrides["repository"]["max_prefs_bytes"] = int(val)
    if val := _get_env("DFE_REPOSITORY_MAX_OBJECT_BYTES"):
        overrides["repository"]["max_object_bytes"] = int(val)

    # Deployment settings
    if val := _get_env("DFE_DEPLOYMENT_CONFIG_DIR"):
        overrides["deployment"]["config_dir"] = val

    # Helm settings
    if val := _get_env("DFE_HELM_OUTPUT_DIR"):
        overrides["helm"]["output_dir"] = val
    if val := _get_env("DFE_HELM_ENVIRONMENT_FILE"):
        overrides["helm"]["environment_file"] = val

    # Auth settings
    if val := _get_env("DFE_AUTH_ENABLED"):
        overrides["auth"]["enabled"] = val.lower() in ("true", "1", "yes")
    if val := _get_env("DFE_AUTH_DIR"):
        overrides["auth"]["auth_dir"] = val
    if val := _get_env("DFE_AUTH_TRUST_PROXY_AUTH_HEADERS"):
        overrides["auth"]["trust_proxy_auth_headers"] = val.lower() in ("true", "1", "yes")

    # Local auth settings (nested under auth.local)
    if val := _get_env("DFE_AUTH_LOCAL_ENABLED"):
        overrides["auth"].setdefault("local", {})["enabled"] = val.lower() in ("true", "1", "yes")
    if val := _get_env("DFE_AUTH_LOCAL_ADMIN_NAME"):
        overrides["auth"].setdefault("local", {})["admin_name"] = val
    if val := _get_env("DFE_AUTH_LOCAL_ADMIN_PASSWORD"):
        overrides["auth"].setdefault("local", {})["admin_password"] = val
    if val := _get_env("DFE_AUTH_LOCAL_OPERATOR_PASSWORD"):
        overrides["auth"].setdefault("local", {})["operator_password"] = val
    if val := _get_env("DFE_AUTH_LOCAL_VIEWER_PASSWORD"):
        overrides["auth"].setdefault("local", {})["viewer_password"] = val
    if val := _get_env("DFE_AUTH_LOCAL_ORG_ID"):
        overrides["auth"].setdefault("local", {})["org_id"] = val
    if val := _get_env("DFE_AUTH_LOCAL_SEED_ACCOUNTS"):
        # A JSON list of {username, password, groups}. Fail loudly on malformed
        # config: a dropped list would silently strip the shared team logins.
        import json

        try:
            seed = json.loads(val)
        except json.JSONDecodeError as exc:
            raise ValueError(f"DFE_AUTH_LOCAL_SEED_ACCOUNTS is not valid JSON: {exc}") from exc
        overrides["auth"].setdefault("local", {})["seed_accounts"] = seed

    # OIDC settings (nested under auth.oidc)
    if val := _get_env("DFE_AUTH_OIDC_PROVIDERS_DIR"):
        overrides["auth"].setdefault("oidc", {})["providers_dir"] = val
    if val := _get_env("DFE_AUTH_OIDC_SYNC_ENABLED"):
        overrides["auth"].setdefault("oidc", {})["sync_enabled"] = val.lower() in (
            "true",
            "1",
            "yes",
        )
    if val := _get_env("DFE_AUTH_OIDC_SYNC_ON_STARTUP"):
        overrides["auth"].setdefault("oidc", {})["sync_on_startup"] = val.lower() in (
            "true",
            "1",
            "yes",
        )

    # Central users+groups store backend (auto default; document nested under auth.accounts_store)
    if val := _get_env("DFE_AUTH_STORE_BACKEND"):
        overrides["auth"]["store_backend"] = val
    if val := _get_env("DFE_AUTH_ACCOUNTS_STORE_URI"):
        overrides["auth"].setdefault("accounts_store", {})["uri"] = val
    if val := _get_env("DFE_AUTH_ACCOUNTS_STORE_DATABASE"):
        overrides["auth"].setdefault("accounts_store", {})["database"] = val
    if val := _get_env("DFE_AUTH_ACCOUNTS_STORE_COLLECTION"):
        overrides["auth"].setdefault("accounts_store", {})["collection"] = val

    # HyperDX settings
    if val := _get_env("DFE_HYPERDX_BASE_URL"):
        overrides["hyperdx"]["base_url"] = val
    if val := _get_env("DFE_HYPERDX_ENABLED"):
        overrides["hyperdx"]["enabled"] = val.lower() in ("true", "1", "yes")
    if val := _get_env("DFE_HYPERDX_API_KEY_ENV"):
        overrides["hyperdx"]["api_key_env"] = val

    # Gitops settings
    if val := _get_env("DFE_GITOPS_ENABLED"):
        overrides["gitops"]["enabled"] = val.lower() in ("true", "1", "yes")
    if val := _get_env("DFE_GITOPS_REPO_URL"):
        overrides["gitops"]["repo_url"] = val
    if val := _get_env("DFE_GITOPS_BRANCH"):
        overrides["gitops"]["branch"] = val
    if val := _get_env("DFE_GITOPS_LOCAL_PATH"):
        overrides["gitops"]["local_path"] = val
    if val := _get_env("DFE_GITOPS_PUSH"):
        overrides["gitops"]["push"] = val.lower() in ("true", "1", "yes")
    if val := _get_env("DFE_GITOPS_USERNAME"):
        overrides["gitops"]["username"] = val
    if val := _get_env("DFE_GITOPS_TOKEN"):
        overrides["gitops"]["token"] = val
    if val := _get_env("DFE_GITOPS_AUTHOR_NAME"):
        overrides["gitops"]["author_name"] = val
    if val := _get_env("DFE_GITOPS_AUTHOR_EMAIL"):
        overrides["gitops"]["author_email"] = val
    if val := _get_env("DFE_GITOPS_MODE"):
        overrides["gitops"]["mode"] = val.strip().lower()
    if val := _get_env("DFE_GITOPS_FORGE_PROVIDER"):
        overrides["gitops"]["forge_provider"] = val.strip().lower()
    if val := _get_env("DFE_GITOPS_FORGE_API_BASE"):
        overrides["gitops"]["forge_api_base"] = val

    # API settings
    if val := _get_env("DFE_API_HOST"):
        overrides["api"]["host"] = val
    if val := _get_env("DFE_API_PORT"):
        overrides["api"]["port"] = int(val)
    if val := _get_env("DFE_API_JWT_SECRET"):
        overrides["api"]["jwt_secret"] = val
    if val := _get_env("DFE_API_SESSION_SECRET"):
        overrides["api"]["session_secret"] = val
    if val := _get_env("DFE_API_CORS_ORIGINS"):
        overrides["api"]["cors_origins"] = [o.strip() for o in val.split(",") if o.strip()]
    if val := _get_env("DFE_API_JWT_EXPIRE_MINUTES"):
        overrides["api"]["jwt_expire_minutes"] = int(val)
    if val := _get_env("DFE_API_ELASTIC_CONVERTER_MAX_UPLOAD_BYTES"):
        overrides["api"]["elastic_converter_max_upload_bytes"] = int(val)
    if val := _get_env("DFE_API_ELASTIC_CONVERTER_READ_CHUNK_SIZE"):
        overrides["api"]["elastic_converter_read_chunk_size"] = int(val)
    if val := _get_env("DFE_API_ELASTIC_CONVERTER_CONTENT_LENGTH_SLACK_BYTES"):
        overrides["api"]["elastic_converter_content_length_slack_bytes"] = int(val)

    # Secrets settings (the scalo.secrets seam for minted secrets)
    if val := _get_env("DFE_SECRETS_PROVIDER"):
        overrides["secrets"]["provider"] = val
    if val := _get_env("DFE_SECRETS_PATH"):
        overrides["secrets"]["path"] = val
    if val := _get_env("DFE_SECRETS_ADDR"):
        overrides["secrets"]["addr"] = val
    if val := _get_env("DFE_SECRETS_MOUNT"):
        overrides["secrets"]["mount"] = val
    if val := _get_env("DFE_SECRETS_ROLE"):
        overrides["secrets"]["role"] = val

    # Deployment posture (production|dev|test|...) - gates the placeholder-secret guard
    if val := _get_env("DFE_ENV"):
        overrides["env"] = val

    # Config directory (dfe-devex submodule) — auto-resolves registry subdirs
    # Individual env vars (DFE_SOURCES_DIR, etc.) take precedence.
    config_dir = _get_env("DFE_CONFIG_DIR")
    if config_dir:
        overrides["config_dir"] = config_dir
        _config_dir_subdirs = {
            ("services", "config_yaml_dir"): "services",
            ("source", "sources_dir"): "sources",
            ("source", "builds_dir"): "source-builds",
            ("source", "plans_dir"): "source-plans",
            ("source", "deploys_dir"): "source-deploys",
            ("fieldmap", "fieldmaps_dir"): "fieldmaps",
            ("deployment", "config_dir"): "deployment",
            ("hunts", "hunt_dir"): "hunts",
            ("hunts", "rule_repo_dir"): "hunt-rules",
            ("hunts", "rules_dir"): "rules",
            ("hunts", "alert_destinations_dir"): "alert-destinations",
            ("query", "yaml_dir"): "queries",
        }
        for (section, key), subdir in _config_dir_subdirs.items():
            if key not in overrides.get(section, {}):
                overrides.setdefault(section, {})[key] = str(Path(config_dir) / subdir)

    # Remove empty sections
    return {k: v for k, v in overrides.items() if v}


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dicts. Override wins on conflicts."""
    import copy

    from dfe_engine.yaml_utils import deep_merge

    result = copy.deepcopy(base)
    deep_merge(result, override)
    return result


def load_settings(config_file: str | None = None) -> DFESettings:
    """
    Load DFE settings with the following precedence:
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


def get_clickhouse_config(settings: DFESettings | None = None) -> dict:
    """
    Get ClickHouse configuration as a dictionary.

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
