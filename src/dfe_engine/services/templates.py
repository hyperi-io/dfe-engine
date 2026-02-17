"""Default configuration template generation for DFE services.

Generates deployment-ready configuration templates for different profiles.
"""

from __future__ import annotations

from typing import Any

from dfe_engine.services.models import (
    SERVICE_CONFIG_CLASSES,
    VALID_SERVICES,
)


def generate_template(service: str, profile: str = "default") -> dict[str, Any]:
    """Generate a configuration template for a service.

    Args:
        service: Service name ('receiver', 'loader', 'archiver')
        profile: Configuration profile
            - 'default': Rust defaults (dev-friendly, localhost)
            - 'production': Production-hardened (TLS, SASL, tuned buffers)
            - 'k8s': Kubernetes-optimized (service DNS, ConfigMap-friendly)

    Returns:
        Configuration dictionary ready for YAML serialization

    Raises:
        ValueError: Unknown service or profile
    """
    if service not in VALID_SERVICES:
        msg = f"Unknown service: {service}. Valid: {', '.join(sorted(VALID_SERVICES))}"
        raise ValueError(msg)

    config_cls = SERVICE_CONFIG_CLASSES[service]

    # Start with Rust defaults
    config = config_cls()
    base = config.model_dump(mode="json")

    # Apply profile overrides
    if profile == "default":
        return base
    elif profile == "production":
        return _apply_production_overrides(service, base)
    elif profile == "k8s":
        return _apply_k8s_overrides(service, base)
    else:
        msg = f"Unknown profile: {profile}. Valid: default, production, k8s"
        raise ValueError(msg)


def _apply_production_overrides(service: str, config: dict) -> dict:
    """Apply production-hardened overrides."""
    if service == "receiver":
        config["server"]["tls"] = {
            "enabled": True,
            "cert_file": "/etc/tls/tls.crt",
            "key_file": "/etc/tls/tls.key",
            "ca_file": "/etc/tls/ca.crt",
            "client_auth": "none",
            "cert_secret": None,
            "key_secret": None,
            "ca_secret": None,
            "refresh_interval_secs": 3600,
        }
        config["server"]["auth"]["mode"] = "bearer"
        config["server"]["auth"]["bearer"]["secret_source"] = "vault:secret/dfe/receiver:bearer_tokens"
        config["kafka"]["sasl"] = {
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
        }
        config["kafka"]["tls"]["enabled"] = True

    elif service == "loader":
        config["kafka"]["sasl"] = {
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
        }
        config["kafka"]["tls"] = {
            "enabled": True,
            "ca_file": "/etc/tls/kafka-ca.crt",
            "cert_file": None,
            "key_file": None,
            "skip_verify": False,
        }
        config["buffer"]["flush_bytes"] = 4_194_304  # 4MB
        config["buffer"]["flush_rows"] = 50_000
        config["logging"]["format"] = "json"

    elif service == "archiver":
        config["kafka"]["security_protocol"] = "SASL_SSL"
        config["kafka"]["sasl_mechanism"] = "SCRAM-SHA-512"
        config["compression"]["codec"] = "zstd"
        config["compression"]["level"] = 6
        config["buffer"]["flush_bytes"] = 128 * 1024 * 1024  # 128MB
        config["buffer"]["writer_parallelism"] = 8

    return config


def _apply_k8s_overrides(service: str, config: dict) -> dict:
    """Apply Kubernetes-optimized overrides."""
    # Common: use K8s service DNS names
    if service == "receiver":
        config["server"]["bind_address"] = "0.0.0.0:8080"
        config["kafka"]["brokers"] = ["kafka-bootstrap.kafka.svc.cluster.local:9092"]
        config["loader"]["address"] = "dfe-loader.dfe.svc.cluster.local:9000"
        config["metrics"]["address"] = "0.0.0.0:9090"
        config["buffer"]["memory_limit"] = 0  # Auto-detect from K8s limits

    elif service == "loader":
        config["kafka"]["brokers"] = ["kafka-bootstrap.kafka.svc.cluster.local:9092"]
        config["clickhouse"]["hosts"] = ["clickhouse.clickhouse.svc.cluster.local:9000"]
        config["metrics"]["address"] = "0.0.0.0:9090"
        config["memory"]["limit_bytes"] = 0  # Auto-detect
        config["logging"]["format"] = "json"

    elif service == "archiver":
        config["kafka"]["brokers"] = ["kafka-bootstrap.kafka.svc.cluster.local:9092"]
        config["metrics"]["address"] = "0.0.0.0:9090"
        config["memory"]["limit_bytes"] = 0  # Auto-detect

    return config
