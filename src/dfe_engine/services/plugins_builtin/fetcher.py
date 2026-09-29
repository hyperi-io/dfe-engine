"""Built-in plugin for dfe-fetcher.

Consolidates descriptor, validation, sizing, and template overrides
for the fetcher service. Type 2 service (multi-source) -- pulls from
SaaS APIs and routes events to Kafka like a receiver.
"""

from typing import Any

from dfe_engine.services.descriptor import KafkaRole, ServiceDescriptor
from dfe_engine.services.plugin import ServicePlugin

descriptor = ServiceDescriptor(
    name="fetcher",
    display_name="DFE Fetcher",
    image="ghcr.io/hyperi-io/dfe-fetcher",
    default_port=8080,
    metrics_port=9090,
    kafka_role=KafkaRole.PRODUCER,
    liveness_paths=("/livez",),
    readiness_paths=("/readyz",),
    description="SaaS API fetcher -- pulls from cloud APIs and routes to Kafka.",
)


def _validate_fetcher(config: Any, errors: list[str], warnings: list[str]) -> None:
    """Cross-field validation for dfe-fetcher config."""
    if not config.kafka.brokers:
        errors.append("kafka.brokers is required for the fetcher")

    # Check source names are unique
    source_names = [s.name for s in config.sources]
    if len(source_names) != len(set(source_names)):
        errors.append("Source names must be unique within a deployment")

    for src in config.sources:
        if not src.source_type:
            errors.append(f"Source '{src.name}' is missing source_type")

        if src.auth.type == "oauth2" and not src.auth.token_url:
            warnings.append(f"Source '{src.name}': OAuth2 auth without token_url configured")


_sizing_overrides: dict[str, dict[str, Any]] = {
    "xs": {},
    "small": {},
    "medium": {},
    "large": {},
    "xlarge": {},
}

_keda_defaults: dict[str, Any] = {
    "min_replicas": 1,
    "max_replicas": 4,
}

_template_overrides: dict[str, dict[str, Any]] = {
    "production": {
        "kafka": {
            "sasl": {
                "enabled": True,
                "mechanism": "scram_sha_512",
                "username": "",
                "password": "",
                "oauth_token_endpoint": None,
                "oauth_client_id": None,
                "oauth_client_secret": None,
                "oauth_scope": None,
                "oauth_extensions": None,
                "aws_region": None,
                "aws_access_key_id": None,
                "aws_secret_access_key": None,
                "aws_session_token": None,
                "aws_profile": None,
            },
            "tls": {"enabled": True},
        },
    },
    "k8s": {
        # Empty: every deployment names its own brokers.
        "kafka": {"brokers": []},
        "metrics": {"address": "0.0.0.0:9090"},
    },
}


def _make_plugin() -> ServicePlugin:
    from dfe_engine.deployment.models.fetcher import FetcherDeploymentConfig
    from dfe_engine.services.models.fetcher import FetcherConfig

    return ServicePlugin(
        descriptor=descriptor,
        config_class=FetcherConfig,
        deployment_class=FetcherDeploymentConfig,
        validate_config=_validate_fetcher,
        sizing_overrides=_sizing_overrides,
        keda_defaults=_keda_defaults,
        default_size="small",
        config_template_overrides=_template_overrides,
    )


def plugin() -> ServicePlugin:
    return _make_plugin()
