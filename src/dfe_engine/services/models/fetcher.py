"""Configuration model for dfe-fetcher.

A fetcher is like a receiver but pulls data from cloud SaaS APIs instead
of receiving data pushed to it. Otherwise the same architecture: routes
events to Kafka topics.

Like transforms, a fetcher can have multiple source deployments — each
pulling from a different SaaS source with its own auth, polling config,
ENV settings, and associated files. CRUD-managed via the engine API.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from dfe_engine.services.models.base import BaseServiceConfig
from dfe_engine.services.models.common import (
    DlqConfig,
    KafkaTlsConfig,
    MemoryConfig,
    SaslConfig,
)
from dfe_engine.services.models.source_common import BaseSourceConfig


# ---------------------------------------------------------------------------
# Fetcher source configs
# ---------------------------------------------------------------------------


class FetcherAuthConfig(BaseModel):
    """Authentication for a SaaS API source."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(
        default="oauth2",
        description="Auth type: oauth2, api_key, bearer, basic, none",
    )
    # OAuth2
    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    token_url: str = ""
    scopes: list[str] = Field(default_factory=list)
    # API key
    api_key: SecretStr = SecretStr("")
    api_key_header: str = "Authorization"
    # Bearer token
    bearer_token: SecretStr = SecretStr("")
    # Basic auth
    username: str = ""
    password: SecretStr = SecretStr("")


class FetcherSourceConfig(BaseSourceConfig):
    """A single SaaS source within a fetcher deployment.

    Extends BaseSourceConfig with fetcher-specific fields:
    - source_type: the SaaS API type (microsoft_graph, okta, crowdstrike, etc.)
    - base_url: API endpoint
    - auth: per-source authentication config
    - poll_interval_secs / batch_size: polling behaviour

    Per-source ENV + files are inherited from BaseSourceConfig.
    CRUD-managed via the engine API.
    """

    source_type: str = Field(
        ..., description="SaaS source type (e.g. 'microsoft_graph', 'okta', 'crowdstrike')"
    )
    base_url: str = Field(default="", description="API base URL")
    auth: FetcherAuthConfig = Field(default_factory=FetcherAuthConfig)
    poll_interval_secs: int = Field(default=300, ge=10, description="Seconds between API polls")
    batch_size: int = Field(default=1000, ge=1, description="Max records per API call")


# ---------------------------------------------------------------------------
# Routing (same as receiver — routes to Kafka topics)
# ---------------------------------------------------------------------------


class FetcherRoutingConfig(BaseModel):
    """Message routing configuration for the fetcher."""

    model_config = ConfigDict(extra="forbid")

    topic_fields: list[str] = Field(
        default_factory=lambda: ["tags.event.category", "event_category"]
    )
    default_topic: str = "unmatched"
    topic_suffix: str = "_land"
    category_to_topic: dict[str, str] = Field(default_factory=dict)
    dlq: DlqConfig = Field(default_factory=DlqConfig)


# ---------------------------------------------------------------------------
# Kafka (producer — fetcher sends to Kafka like receiver)
# ---------------------------------------------------------------------------


class FetcherKafkaConfig(BaseModel):
    """Kafka producer configuration for the fetcher."""

    model_config = ConfigDict(extra="forbid")

    brokers: list[str] = Field(default_factory=list)
    client_id: str = "dfe-fetcher"
    sasl: SaslConfig | None = None
    tls: KafkaTlsConfig = Field(default_factory=KafkaTlsConfig)
    compression: str = "lz4"
    batch_size: int = Field(default=8 * 1024 * 1024, gt=0)
    linger_ms: int = Field(default=20, ge=0)


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


class FetcherConfig(BaseServiceConfig):
    """Complete configuration for dfe-fetcher.

    Like a receiver but pulls from SaaS APIs instead of receiving HTTP pushes.
    Supports multiple source deployments, each CRUD-managed via the engine API.
    Per-source ENV + files inherited via BaseSourceConfig.
    """

    sources: list[FetcherSourceConfig] = Field(
        default_factory=list,
        description="List of SaaS source configurations",
    )
    kafka: FetcherKafkaConfig = Field(default_factory=FetcherKafkaConfig)
    routing: FetcherRoutingConfig = Field(default_factory=FetcherRoutingConfig)
    extra_env: dict[str, str] = Field(
        default_factory=dict,
        description="Additional environment variables for the fetcher process",
    )
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
