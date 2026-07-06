#  Project:      dfe-engine
#  File:         settings.py
#  Purpose:      Centralized configuration management with Pydantic
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""
DFE Engine Settings Module

Provides centralized configuration management using scalo settings cascade.
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
- DFE_CLICKHOUSE_CONNECTIONS_MAX -> clickhouse.connections_max

Hunts:
- DFE_HUNTS_DIR -> hunts.hunt_dir
- DFE_HUNTS_RULE_REPO_DIR -> hunts.rule_repo_dir
- DFE_HUNTS_RULES_DIR -> hunts.rules_dir

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
from typing import Literal

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
        default="",
        description=(
            "Database where DFE data tables live (landing table, per-source tables). "
            "Falls back to `database` when empty. Lets the connection authenticate "
            "against one database while DFE tables are qualified against another -- "
            "read it via `effective_data_database`, never directly."
        ),
    )
    landing_table: str = Field(
        default="default",
        description="Catch-all table where un-split source data lands (db.landing_table)",
    )
    secure: bool = Field(default=True)
    verify: bool = Field(default=False)
    connections_max: int = Field(default=300)
    # Deployment topology: "single" (standalone CH -> MergeTree DDL) or
    # "replicated" (cluster CH + Keeper -> ReplicatedMergeTree + ON CLUSTER).
    topology: str = Field(default="single")

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
    """Hunt directory settings.

    Only the directory pointers are consumed (the hunt_runner reads hunt_dir; the
    API registries read rules_dir + alert_destinations_dir). The former scheduler /
    checkpoint / inline-alert fields were removed with the legacy hunt modules
    (alert/alert_grouping/checkpoint/scheduler) - they had no reader (P3.16).
    """

    hunt_dir: str = Field(default="", description="Directory containing hunt YAML configs")
    rule_repo_dir: str = Field(default="", description="Directory containing Jinja2 rule templates")
    rules_dir: str = Field(
        default="",
        description="YAML directory for API-managed detection rules (DirectoryConfigStore SSoT)",
    )
    alert_destinations_dir: str = Field(
        default="",
        description="YAML directory for alert destination definitions (DirectoryConfigStore SSoT)",
    )


class ArtifactorySettings(BaseModel):
    """Artifactory settings for downloading templates."""

    url: str = Field(default="")
    username: str = Field(default="")
    password: str = Field(default="")
    templates_version: str = Field(default="latest")


class KafkaSettings(BaseModel):
    """Kafka connection settings.

    SASL fields are only needed where the engine itself talks to the brokers -
    today that is the sampler's Kafka consumer (recent-tail + logreducer
    KafkaSource). DFE-owned brokers run SASL/SCRAM-SHA-512, so leave the
    mechanism empty for a PLAINTEXT dev broker and set it (with username +
    password) for a real cluster.
    """

    bootstrap_servers: str = Field(default="localhost:9092")
    security_protocol: str = Field(default="PLAINTEXT")
    sasl_mechanism: str = Field(
        default="",
        description="librdkafka sasl.mechanism (e.g. SCRAM-SHA-512); empty = no SASL",
    )
    sasl_username: str = Field(default="", description="SASL username")
    sasl_password: str = Field(default="", description="SASL password")


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
    """Source-sampling settings (the /sources/{source}/sample API + `dfe-api sample`).

    Two families of mode: cheap "recent"/"random" reads that run inline, and the
    memory-hungry logreducer modes ("smart"/"anomaly") that are gated. logreducer
    is memory-hungry, so its concurrency is capped at ``max_concurrent`` instances,
    each bounded to ``max_memory_gb`` - both small by default (Derek, 2026-07-01).

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
    """

    sources_dir: str = Field(default="", description="YAML directory for Source definitions (SSoT)")


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

    Only the transform-WASM endpoints + the config replica dir are consumed. The
    per-service *_url health/metrics endpoints were removed with ServiceStateClient
    (services/state.py, deleted) - they had no reader (P3.17).

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
    - DFE_HELM_ENVIRONMENT_FILE -> helm.environment_file
    """

    environment_file: str = Field(default="", description="Path to environment config YAML")


class OIDCSettings(BaseModel):
    """OIDC provider settings.

    Environment variables:
    - DFE_AUTH_OIDC_PROVIDERS_DIR -> auth.oidc.providers_dir
    """

    providers_dir: str = Field(default="", description="OIDC provider config directory")


class LocalAuthSettings(BaseModel):
    """Built-in local-account toggle (nested under auth.local).

    Only ``enabled`` is consumed (echoed in system info). The admin account is
    seeded by bootstrap_auth from DFE_ADMIN_PASSWORD, not from here.
    """

    enabled: bool = Field(default=False, description="Seed built-in local accounts")


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
    local: LocalAuthSettings = Field(default_factory=LocalAuthSettings)


class HyperDXSettings(BaseModel):
    """HyperDX integration settings.

    Environment variables:
    - DFE_HYPERDX_BASE_URL -> hyperdx.base_url
    - DFE_HYPERDX_ENABLED -> hyperdx.enabled
    - DFE_HYPERDX_API_KEY_ENV -> hyperdx.api_key_env
    - DFE_HYPERDX_PER_GROUP -> hyperdx.per_group
    - DFE_GA_TEAM_NAME -> hyperdx.ga_team_name
    """

    base_url: str = Field(default="", description="HyperDX API base URL")
    api_key_env: str = Field(
        default="DFE_HYPERDX_API_KEY",
        description="Env var for HyperDX API key",
    )
    enabled: bool = Field(default=False, description="Enable HyperDX integration")
    per_group: bool = Field(
        default=False,
        description=(
            "Team model posture. False (GA default) = ONE shared HyperDX team "
            "(ga_team_name) that every provisioned user joins; tenant isolation is "
            "the per-connection DFE_current_tenant_id setting, not the team. True "
            "(post-GA) = the richer per-org team (customer-<org>) per org."
        ),
    )
    ga_team_name: str = Field(
        default="dfe",
        description="Name of the single shared HyperDX team used when per_group is False (GA).",
    )


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


# Known placeholder JWT secret - fine for local dev, REJECTED in a production
# posture when auth is on (see DFESettings._reject_placeholder_secret).
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
            "JWT signing secret (HS256), >= 32 bytes. This dev default is a KNOWN "
            "placeholder - operators MUST override it via DFE_API_JWT_SECRET in "
            "production, else tokens can be forged."
        ),
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
    (deferred). See docs/BACKING-SERVICES.md.

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
    def _reject_placeholder_secret(self) -> "DFESettings":
        # Fail fast if a production deployment turns auth on but never overrode
        # the known dev jwt_secret - otherwise anyone can forge tokens. Local dev
        # opts out via DFE_ENV. Minimum length is enforced on the field itself.
        is_prod = not is_dev_posture(self.env)
        if is_prod and self.auth.enabled and self.api.jwt_secret == _DEV_JWT_SECRET:
            raise ValueError(
                "api.jwt_secret is the known dev placeholder but env is "
                f"'{self.env}' with auth enabled; set a strong DFE_API_JWT_SECRET "
                "(or DFE_ENV=dev for local development)"
            )
        return self

    @model_validator(mode="after")
    def _warn_auth_disabled_in_production(self) -> "DFESettings":
        # A non-dev posture with auth OFF means get_current_user falls to path 4
        # and treats EVERY credential-less request as an anonymous admin - the same
        # class of fail-open the jwt_secret guard prevents. We cannot hard-fail like
        # that guard does: auth.enabled defaults False AND env defaults 'production',
        # so this is the DEFAULT combo and raising would break every bare
        # DFESettings()/load_settings() construction. Surface it LOUD instead so a
        # real deploy that forgets DFE_AUTH_ENABLED=true cannot run wide open
        # silently (F-AUTH-DEFAULT-OFF).
        if not is_dev_posture(self.env) and not self.auth.enabled:
            from scalo.logger import logger

            logger.warning(
                f"auth.enabled is FALSE in a non-dev posture (DFE_ENV={self.env!r}): "
                "every request runs as an anonymous admin. Set DFE_AUTH_ENABLED=true "
                "for any real deployment (or DFE_ENV=dev for local development)."
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
    overrides = {
        "clickhouse": {},
        "hunts": {},
        "artifactory": {},
        "kafka": {},
        "storage": {},
        "query": {},
        "query_views": {},
        "sampler": {},
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
    if val := _get_env("DFE_CLICKHOUSE_LANDING_TABLE", "CLICKHOUSE_LANDING_TABLE"):
        overrides["clickhouse"]["landing_table"] = val
    if val := _get_env("DFE_CLICKHOUSE_SECURE", "CLICKHOUSE_SECURE"):
        overrides["clickhouse"]["secure"] = val.lower() in ("true", "1", "yes")
    if val := _get_env("DFE_CLICKHOUSE_VERIFY", "CLICKHOUSE_VERIFY"):
        overrides["clickhouse"]["verify"] = val.lower() in ("true", "1", "yes")
    if val := _get_env("DFE_CLICKHOUSE_CONNECTIONS_MAX"):
        overrides["clickhouse"]["connections_max"] = int(val)
    if val := _get_env("DFE_CLICKHOUSE_TOPOLOGY"):
        overrides["clickhouse"]["topology"] = val

    # Hunts settings (only the directory pointers remain; the scheduler/checkpoint/
    # inline-alert settings were removed with the legacy hunt modules - P3.16).
    if val := _get_env("DFE_HUNTS_DIR"):
        overrides["hunts"]["hunt_dir"] = val
    if val := _get_env("DFE_HUNTS_RULE_REPO_DIR"):
        overrides["hunts"]["rule_repo_dir"] = val
    if val := _get_env("DFE_HUNTS_RULES_DIR"):
        overrides["hunts"]["rules_dir"] = val

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
    if val := _get_env("DFE_KAFKA_SASL_MECHANISM", "KAFKA_SASL_MECHANISM"):
        overrides["kafka"]["sasl_mechanism"] = val
    if val := _get_env("DFE_KAFKA_SASL_USERNAME", "KAFKA_SASL_USERNAME"):
        overrides["kafka"]["sasl_username"] = val
    if val := _get_env("DFE_KAFKA_SASL_PASSWORD", "KAFKA_SASL_PASSWORD"):
        overrides["kafka"]["sasl_password"] = val

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

    # Schemas settings (dfe-schemas submodule)
    if val := _get_env("DFE_SCHEMAS_DIR"):
        overrides["schemas"]["schemas_dir"] = val

    # Source settings
    if val := _get_env("DFE_SOURCES_DIR"):
        overrides["source"]["sources_dir"] = val

    # FieldMap settings
    if val := _get_env("DFE_FIELDMAPS_DIR"):
        overrides["fieldmap"]["fieldmaps_dir"] = val

    # Services settings (the per-service health/metrics *_url vars were removed
    # with ServiceStateClient - P3.17; only the transform-WASM + config dir remain).
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

    # OIDC settings (nested under auth.oidc)
    if val := _get_env("DFE_AUTH_OIDC_PROVIDERS_DIR"):
        overrides["auth"].setdefault("oidc", {})["providers_dir"] = val

    # HyperDX settings
    if val := _get_env("DFE_HYPERDX_BASE_URL"):
        overrides["hyperdx"]["base_url"] = val
    if val := _get_env("DFE_HYPERDX_ENABLED"):
        overrides["hyperdx"]["enabled"] = val.lower() in ("true", "1", "yes")
    if val := _get_env("DFE_HYPERDX_API_KEY_ENV"):
        overrides["hyperdx"]["api_key_env"] = val
    if val := _get_env("DFE_HYPERDX_PER_GROUP"):
        overrides["hyperdx"]["per_group"] = val.lower() in ("true", "1", "yes")
    if val := _get_env("DFE_GA_TEAM_NAME"):
        overrides["hyperdx"]["ga_team_name"] = val

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
    """Deep merge two dicts for settings precedence; *override* wins.

    Unlike yaml_utils.deep_merge (which APPENDS lists), a list in *override*
    REPLACES the one in *base*. Settings precedence is strict override
    ("ENV > config file > defaults"): a list-valued setting (api.cors_origins,
    hunts.alert_channels) supplied at a higher tier must SUPERSEDE the lower
    tier, not concatenate onto it. This is scoped to the settings load path;
    yaml_utils.deep_merge (used by the Helm/overlay compilers) keeps appending.
    """
    import copy

    result = copy.deepcopy(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
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
