"""Built-in plugin for dfe-loader.

Consolidates loader-specific descriptor, validation, and template overrides.
"""

from typing import Any

from dfe_engine.services.descriptor import ServiceDescriptor
from dfe_engine.services.plugin import ServicePlugin

descriptor = ServiceDescriptor(
    name="loader",
    display_name="DFE Loader",
    description="Kafka consumer or gRPC server -- loads events into ClickHouse.",
)


def _check_listen_port(listen: str, errors: list[str]) -> None:
    """Refuse a listen port the receiver will not dial.

    The engine compiles the receiver's destination from the app manifest's push
    port but takes ``grpc.listen`` from whatever the base config says, so the two
    ends of one connection had different authors and could disagree. They did,
    for three days: the receiver answered HTTP 202 and every record was dropped
    into a closed port with nothing logged.
    """
    from dfe_engine.appmgmt import catalogue

    push = catalogue.descriptor("dfe-loader").endpoints.get(catalogue.PUSH_ENDPOINT)
    if push is None:
        errors.append(
            f"the app manifest declares no {catalogue.PUSH_ENDPOINT!r} endpoint for "
            "dfe-loader, so no sender dials grpc.listen"
        )
        return
    _, _, port = listen.rpartition(":")
    if not port.isdigit():
        errors.append(f"grpc.listen {listen!r} must end in a port number")
        return
    if int(port) != push.port:
        errors.append(
            f"grpc.listen is on port {port}, but dfe-receiver is told to dial "
            f"{push.port} -- the engine compiles that endpoint from the app "
            f"manifest's push port, so a different listen port drops every record "
            f"behind an HTTP 202."
        )


def _validate_loader(config: Any, errors: list[str], warnings: list[str]) -> None:
    """Cross-field validation for dfe-loader config.

    Mirrors dfe-loader/src/config/loader.rs Config::validate(), scoped by the
    transport the config selects.

    The Rust validate() checks kafka.brokers unconditionally, but that check is
    unreachable for a gRPC deployment: serde fills kafka.brokers from
    KafkaConfig::default() (["localhost:9092"]), so a grpc-transport YAML never
    presents an empty broker list to the binary. The engine AUTHORS configs, so
    the broker requirement is scoped to the transport that actually consumes it
    -- otherwise a legitimate grpc-only config is rejected at author time.

    The grpc.listen requirement is not in the Rust validate() either, but scalo
    fails the first recv() with "no listen address configured for receiving"
    (scalo-rs/src/transport/grpc/mod.rs:652-655) when listen is unset. Catching
    it here turns a runtime pod failure into an author-time error.
    """
    transport = config.transport.lower()

    if transport == "kafka":
        if not config.kafka.brokers:
            errors.append("At least one Kafka broker must be configured")

        # An empty kafka.topics is valid: the loader auto-discovers every *_load/*_land topic.

        if config.kafka.sasl and config.kafka.sasl.enabled:
            mechanism = config.kafka.sasl.mechanism.lower().replace("-", "_")
            if mechanism in ("plain", "scram_sha_256", "scram_sha_512"):
                if not config.kafka.sasl.username:
                    errors.append(f"SASL {mechanism} requires username")
                if not config.kafka.sasl.password.get_secret_value():
                    errors.append(f"SASL {mechanism} requires password")
            elif mechanism == "oauthbearer":
                if not config.kafka.sasl.oauth_token_endpoint:
                    errors.append("SASL OAUTHBEARER requires oauth_token_endpoint")
                if not config.kafka.sasl.oauth_client_id:
                    errors.append("SASL OAUTHBEARER requires oauth_client_id")
            elif mechanism == "aws_msk_iam":
                if not config.kafka.sasl.aws_region:
                    errors.append("SASL AWS_MSK_IAM requires aws_region")

    elif transport == "grpc":
        if not config.grpc.listen:
            errors.append("grpc.listen is required when transport is 'grpc'")
        else:
            _check_listen_port(config.grpc.listen, errors)

    if not config.clickhouse.hosts:
        errors.append("At least one ClickHouse host must be configured")

    if config.buffer.flush_bytes == 0:
        errors.append("buffer.flush_bytes must be greater than 0")

    if config.buffer.flush_rows == 0:
        errors.append("buffer.flush_rows must be greater than 0")

    # The loader resolves the database by looking the FIRST db_fields hit up in
    # org_routes (dfe-loader/src/routing/router.rs:240-252) -- with db_fields
    # empty it short-circuits to default_db and never consults the map at all.
    if config.routing.org_routes and not config.routing.db_fields:
        warnings.append(
            "routing.org_routes is set but routing.db_fields is empty -- the loader "
            "reads the org from the first db_fields hit, so every route is dead and "
            "all data lands in default_db"
        )


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
            "tls": {
                "enabled": True,
                "ca_cert_file": "/etc/tls/kafka-ca.crt",
                "cert_file": None,
                "key_file": None,
                "skip_verify": False,
            },
        },
        "buffer": {"flush_bytes": 4_194_304, "flush_rows": 50_000},
        "logging": {"format": "json"},
    },
    "k8s": {
        # Empty: every deployment names its own brokers and ClickHouse hosts.
        "kafka": {"brokers": []},
        "clickhouse": {"hosts": []},
        "metrics": {"address": "0.0.0.0:9090"},
        "memory": {"limit_bytes": 0},
        "logging": {"format": "json"},
    },
}


def _make_plugin() -> ServicePlugin:
    from dfe_engine.services.models.loader import LoaderConfig

    return ServicePlugin(
        descriptor=descriptor,
        config_class=LoaderConfig,
        validate_config=_validate_loader,
        config_template_overrides=_template_overrides,
    )


def plugin() -> ServicePlugin:
    """The loader plugin, loaded through the ``dfe_engine.services`` entry point."""
    return _make_plugin()
