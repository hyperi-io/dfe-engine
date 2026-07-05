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
- DFE_KAFKA_BOOTSTRAP_SERVERS (legacy: KAFKA_BOOTSTRAP_SERVERS) -> kafka.bootstrap_servers
- DFE_KAFKA_SECURITY_PROTOCOL (legacy: KAFKA_SECURITY_PROTOCOL) -> kafka.security_protocol

Schemas:
- DFE_SCHEMAS_DIR -> schemas.schemas_dir (dfe-schemas submodule root)

Auth (local):
- DFE_AUTH_LOCAL_ENABLED -> auth.local.enabled
- DFE_AUTH_LOCAL_ADMIN_PASSWORD -> auth.local.admin_password
- DFE_AUTH_LOCAL_OPERATOR_PASSWORD -> auth.local.operator_password
- DFE_AUTH_LOCAL_VIEWER_PASSWORD -> auth.local.viewer_password
- DFE_AUTH_LOCAL_ORG_ID -> auth.local.org_id

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

from pydantic import BaseModel, Field

from .yaml_utils import yaml_load


def _get_env(primary: str, *fallbacks: str) -> str | None:
    """Get env var with fallback support. Primary (DFE_*) takes precedence over legacy names."""
    if val := os.getenv(primary):
        return val
    for fallback in fallbacks:
        if val := os.getenv(fallback):
            return val
    return None


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
        description="Create the DFE database, landing table, and hunt results table on startup",
    )
    secure: bool = Field(default=True)
    verify: bool = Field(default=False)
    connections_min: int = Field(default=10)
    connections_max: int = Field(default=300)

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


class KafkaSettings(BaseModel):
    """Kafka connection settings."""

    bootstrap_servers: str = Field(default="localhost:9092")
    security_protocol: str = Field(default="PLAINTEXT")


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
        default="dfe_query_user", description="Username for restricted query user"
    )
    restricted_password: str = Field(default="", description="Password for restricted query user")
    auto_bootstrap: bool = Field(
        default=True,
        description="Automatically bootstrap RBAC and builtin views on startup",
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
    """Endpoints for managed DFE services.

    Used by ServiceStateClient to query health and metrics from running services.

    Environment variables:
    - DFE_SERVICES_RECEIVER_URL -> services.receiver_url
    - DFE_SERVICES_RECEIVER_METRICS_URL -> services.receiver_metrics_url
    - DFE_SERVICES_LOADER_URL -> services.loader_url
    - DFE_SERVICES_ARCHIVER_METRICS_URL -> services.archiver_metrics_url
    - DFE_SERVICES_TRANSFORM_VECTOR_URL -> services.transform_vector_url
    - DFE_SERVICES_TRANSFORM_WASM_URL -> services.transform_wasm_url
    - DFE_SERVICES_FETCHER_URL -> services.fetcher_url
    - DFE_SERVICES_CONFIG_YAML_DIR -> services.config_yaml_dir
    - DFE_SERVICES_TRANSFORM_WASM_COMPILER_URL -> services.transform_wasm_compiler_url
    """

    receiver_url: str = Field(default="http://localhost:8080")
    receiver_metrics_url: str = Field(default="http://localhost:9090")
    loader_url: str = Field(default="http://localhost:9090")
    archiver_metrics_url: str = Field(default="http://localhost:9090")
    transform_vector_url: str = Field(default="http://localhost:8080")
    transform_wasm_url: str = Field(default="http://localhost:8080")
    transform_wasm_compiler_url: str = Field(default="http://localhost:8090")
    fetcher_url: str = Field(default="http://localhost:8080")
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


class AuthSettings(BaseModel):
    """Authorization settings.

    Bespoke role→permission RBAC. Zero external dependencies.

    Environment variables:
    - DFE_AUTH_ENABLED -> auth.enabled
    - DFE_AUTH_DIR -> auth.auth_dir
    """

    enabled: bool = Field(
        default=False,
        description="Enable authorization (default off for dev/test)",
    )
    auth_dir: str = Field(
        default="",
        description="Auth config directory (accounts, groups, api-keys)",
    )
    oidc: OIDCSettings = Field(default_factory=OIDCSettings)


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
        default="dev-secret-key-change-in-production",
        description="JWT signing secret (HS256). Change in production!",
    )
    jwt_algorithm: str = Field(default="HS256", description="JWT algorithm")
    jwt_expire_minutes: int = Field(default=60, description="JWT token expiry in minutes")
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
    schemas: SchemasSettings = Field(default_factory=SchemasSettings)
    source: SourceSettings = Field(default_factory=SourceSettings)
    fieldmap: FieldMapSettings = Field(default_factory=FieldMapSettings)
    services: ServicesSettings = Field(default_factory=ServicesSettings)
    deployment: DeploymentSettings = Field(default_factory=DeploymentSettings)
    helm: HelmSettings = Field(default_factory=HelmSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    hyperdx: HyperDXSettings = Field(default_factory=HyperDXSettings)
    api: APISettings = Field(default_factory=APISettings)


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
    overrides = {
        "clickhouse": {},
        "hunts": {},
        "artifactory": {},
        "kafka": {},
        "storage": {},
        "query": {},
        "query_views": {},
        "schemas": {},
        "source": {},
        "fieldmap": {},
        "services": {},
        "deployment": {},
        "helm": {},
        "auth": {},
        "hyperdx": {},
        "api": {},
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
    if val := _get_env("DFE_CLICKHOUSE_CONNECTIONS_MIN"):
        overrides["clickhouse"]["connections_min"] = int(val)
    if val := _get_env("DFE_CLICKHOUSE_CONNECTIONS_MAX"):
        overrides["clickhouse"]["connections_max"] = int(val)

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
    if val := _get_env("DFE_KAFKA_BOOTSTRAP_SERVERS", "KAFKA_BOOTSTRAP_SERVERS"):
        overrides["kafka"]["bootstrap_servers"] = val
    if val := _get_env("DFE_KAFKA_SECURITY_PROTOCOL", "KAFKA_SECURITY_PROTOCOL"):
        overrides["kafka"]["security_protocol"] = val

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
    if val := _get_env("DFE_SERVICES_RECEIVER_URL"):
        overrides["services"]["receiver_url"] = val
    if val := _get_env("DFE_SERVICES_RECEIVER_METRICS_URL"):
        overrides["services"]["receiver_metrics_url"] = val
    if val := _get_env("DFE_SERVICES_LOADER_URL"):
        overrides["services"]["loader_url"] = val
    if val := _get_env("DFE_SERVICES_ARCHIVER_METRICS_URL"):
        overrides["services"]["archiver_metrics_url"] = val
    if val := _get_env("DFE_SERVICES_TRANSFORM_VECTOR_URL"):
        overrides["services"]["transform_vector_url"] = val
    if val := _get_env("DFE_SERVICES_TRANSFORM_WASM_URL"):
        overrides["services"]["transform_wasm_url"] = val
    if val := _get_env("DFE_SERVICES_FETCHER_URL"):
        overrides["services"]["fetcher_url"] = val
    if val := _get_env("DFE_SERVICES_CONFIG_YAML_DIR"):
        overrides["services"]["config_yaml_dir"] = val

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

    # Local auth settings (nested under auth.local)
    if val := _get_env("DFE_AUTH_LOCAL_ENABLED"):
        overrides["auth"].setdefault("local", {})["enabled"] = val.lower() in ("true", "1", "yes")
    if val := _get_env("DFE_AUTH_LOCAL_ADMIN_PASSWORD"):
        overrides["auth"].setdefault("local", {})["admin_password"] = val
    if val := _get_env("DFE_AUTH_LOCAL_OPERATOR_PASSWORD"):
        overrides["auth"].setdefault("local", {})["operator_password"] = val
    if val := _get_env("DFE_AUTH_LOCAL_VIEWER_PASSWORD"):
        overrides["auth"].setdefault("local", {})["viewer_password"] = val
    if val := _get_env("DFE_AUTH_LOCAL_ORG_ID"):
        overrides["auth"].setdefault("local", {})["org_id"] = val

    # HyperDX settings
    if val := _get_env("DFE_HYPERDX_BASE_URL"):
        overrides["hyperdx"]["base_url"] = val
    if val := _get_env("DFE_HYPERDX_ENABLED"):
        overrides["hyperdx"]["enabled"] = val.lower() in ("true", "1", "yes")
    if val := _get_env("DFE_HYPERDX_API_KEY_ENV"):
        overrides["hyperdx"]["api_key_env"] = val

    # API settings
    if val := _get_env("DFE_API_HOST"):
        overrides["api"]["host"] = val
    if val := _get_env("DFE_API_PORT"):
        overrides["api"]["port"] = int(val)
    if val := _get_env("DFE_API_JWT_SECRET"):
        overrides["api"]["jwt_secret"] = val
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
