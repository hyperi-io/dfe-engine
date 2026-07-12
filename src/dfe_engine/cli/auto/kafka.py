#  Project:      dfe-engine
#  File:         cli/auto/kafka.py
#  Purpose:      `dfe kafka` - emit ready-to-use Kafka client config for a
#                provider so kcat / confluent / any librdkafka tool "just works".
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""``dfe kafka`` - Kafka provider tooling.

Headline command ``dfe kafka client-config``: connecting a CLI to Kafka is a
genuine PITA (bootstrap + security.protocol + sasl.mechanism + JAAS/creds all
have to line up per provider). This emits the correct librdkafka config for a
provider so you point a tool at it and it works:

    dfe kafka client-config --provider redpanda --tool kcat --out ~/.config/kcat.conf
    kcat -t dfe-events -C                 # just works

    # ephemeral, creds never land in a tracked file:
    eval "$(dfe kafka client-config --provider confluent-cloud --eval)"
    kcat -t dfe-events -C                 # just works (KCAT_CONFIG points at a 0600 temp file)

The emitted shape is the STANDARD librdkafka ``.properties`` file (bootstrap.servers
+ security.protocol + sasl.mechanisms + creds) that every librdkafka tool reads -
kcat via ``-F``/``$KCAT_CONFIG``, the confluent CLI, rpk, plain clients. The provider
DERIVES security.protocol + sasl.mechanism (SCRAM for owned/Redpanda/MSK, PLAIN for
Confluent Cloud); we never hand-set them. Creds come from the configured secrets
source (``.env`` / OpenBao via settings) unless passed explicitly.

The emitters live in ``scalo.kafka.toolconfig`` (one source of provider truth, many
tools). ``dfe kafka topics`` (below) is explicit topic CRUD - DFE creates the
topics its sources need rather than relying on broker
``auto.create.topics.enable``. ``dfe kafka lifecycle`` (mounted at the bottom of
this module from ``cli/kafka_lifecycle.py``) is the managed-cluster up/down/
status seam (WS-C, dfe-engine#99) - see that module for detail.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from typing import Any

import click


def _emitters():
    """Lazy-import the scalo tool-config emitters.

    They ship in ``scalo.kafka.toolconfig`` (scalo-py). Until dfe-engine's scalo
    pin bumps to the version carrying them, this raises a clean, actionable error
    rather than an ImportError traceback.
    """
    try:
        from scalo.kafka.toolconfig import (
            Connection,
            emit_kafbat_cluster,
            emit_kcat,
            emit_librdkafka_properties,
        )
    except ImportError as exc:  # pragma: no cover - exercised once scalo lags
        raise click.ClickException(
            "dfe kafka client-config needs scalo.kafka.toolconfig (scalo with the "
            "Kafka provider abstraction). Bump the scalo pin once scalo-py has "
            "published, then re-run."
        ) from exc
    return Connection, emit_librdkafka_properties, emit_kcat, emit_kafbat_cluster


def _write_600(body: str, path: str) -> str:
    """Write ``body`` to ``path`` with 0600 perms (creds must not be world-readable)."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


@click.group(name="kafka", no_args_is_help=True)
def kafka_group() -> None:
    """Kafka provider tooling: emit client config, manage topics, (soon) clusters."""


@kafka_group.command("client-config")
@click.option(
    "--provider",
    required=True,
    help="Provider: strimzi / redpanda / redpanda-cloud / confluent-cloud / msk / plaintext.",
)
@click.option(
    "--tool",
    type=click.Choice(["properties", "kcat", "kafbat"]),
    default="properties",
    help="Output shape: librdkafka .properties (default), kcat -F file (same), or kafbat-ui JSON.",
)
@click.option("--bootstrap", default=None, help="Bootstrap servers (else from settings/.env).")
@click.option("--username", default=None, help="SASL username (else from settings/.env).")
@click.option("--password", default=None, help="SASL password (else from settings/.env).")
@click.option("--schema-registry-url", default=None, help="Schema registry URL (kafbat only).")
@click.option(
    "--out", default=None, metavar="FILE", help="Write to FILE (chmod 0600) instead of stdout."
)
@click.option(
    "--eval",
    "eval_",
    is_flag=True,
    help='Print `export KCAT_CONFIG=<0600 temp file>` for `eval "$(...)"` so kcat just works.',
)
def client_config_cmd(
    provider: str,
    tool: str,
    bootstrap: str | None,
    username: str | None,
    password: str | None,
    schema_registry_url: str | None,
    out: str | None,
    eval_: bool,
) -> None:
    """Emit ready-to-use client config for PROVIDER (kcat / confluent / librdkafka)."""
    from dfe_engine.settings import get_settings

    ks = get_settings().kafka
    bootstrap = bootstrap or ks.bootstrap_servers
    username = username if username is not None else ks.sasl_username
    password = password if password is not None else ks.sasl_password

    Connection, emit_properties, emit_kcat, emit_kafbat = _emitters()
    conn = Connection(
        bootstrap_servers=bootstrap,
        username=username or "",
        password=password or "",
        schema_registry_url=schema_registry_url or "",
    )

    if tool == "kafbat":
        # kafbat-ui cluster config (a dict) - JSON, for pasting into a clusters list.
        click.echo(json.dumps(emit_kafbat(provider, conn), indent=2))
        return

    body = emit_kcat(provider, conn)  # kcat + properties are the same librdkafka body

    if eval_:
        fd, path = tempfile.mkstemp(prefix="dfe-kafka-", suffix=".properties")
        os.close(fd)
        _write_600(body, path)
        # Eval-able: sets KCAT_CONFIG so a bare `kcat -t <topic>` reads this config.
        click.echo(f"export KCAT_CONFIG={path}")
        click.echo(f"# eval this, then: kcat -t <topic> -C   (config at {path}, 0600)", err=True)
        return

    if out:
        _write_600(body, out)
        click.echo(f"wrote {out} (0600). Now: kcat -F {out} -t <topic> -C", err=True)
        return

    click.echo(body, nl=False)


# =============================================================================
# `dfe kafka topics` - explicit topic CRUD, no broker auto-create
# =============================================================================
#
# DFE creates the topics its sources need EXPLICITLY rather than relying on
# `auto.create.topics.enable` (a broker default that silently creates
# mis-partitioned, unmanaged topics on first produce - the anti-pattern this
# group exists to replace). dfe-engine#97.
#
# scalo.kafka.admin.KafkaAdmin (scalo-py) is the published admin primitive for
# topic CONFIG changes (retention / cleanup.policy / partition increase /
# consumer-group offset resets) but, as of the scalo pin here, does NOT expose
# topic create/delete/list - only config-alter operations on topics that
# already exist. `_TopicAdmin` below fills that gap directly against the same
# underlying confluent_kafka AdminClient KafkaAdmin itself wraps, built via the
# identical config shape (bootstrap + security.protocol + sasl.mechanisms +
# creds, provider-derived). Collapse onto `KafkaAdmin.list_topics` /
# `.create_topics` / `.delete_topics` once scalo grows that surface.


def _admin_imports():
    """Lazy-import confluent_kafka's admin client (optional dep, same
    convention as ``_emitters()`` above and ``sampling.kafka_reader``):
    confluent-kafka is NOT locked in ``pyproject.toml`` so a default
    dfe-engine install still runs; `dfe kafka topics` needs it installed
    (``uv pip install confluent-kafka``, or scalo's ``kafka`` extra).
    """
    try:
        from confluent_kafka.admin import AdminClient, NewTopic
    except ImportError as exc:  # pragma: no cover - exercised only without the optional dep
        raise click.ClickException(
            "dfe kafka topics needs confluent-kafka (optional dep). Install it, "
            "e.g. `uv pip install confluent-kafka`."
        ) from exc
    return AdminClient, NewTopic


def _admin_config(
    *,
    bootstrap: str | None,
    provider: str | None,
    username: str | None,
    password: str | None,
) -> dict[str, Any]:
    """Build the librdkafka admin config: bootstrap + security.protocol +
    sasl.mechanisms + creds, sourced from settings unless overridden - the
    same shape ``client-config`` emits. The provider DERIVES protocol +
    mechanism via the credential contract (dfe-engine#98, ``kafka.contract``);
    never hand-set the mechanism.
    """
    from dfe_engine.kafka import contract
    from dfe_engine.settings import get_settings

    ks = get_settings().kafka
    bootstrap = bootstrap or ks.bootstrap_servers
    provider = provider if provider is not None else ks.provider
    username = username if username is not None else ks.sasl_username
    password = password if password is not None else ks.sasl_password

    protocol, mechanism = (
        contract.derive(provider)
        if provider
        else (
            ks.security_protocol,
            ks.sasl_mechanism,
        )
    )

    conf: dict[str, Any] = {"bootstrap.servers": bootstrap}
    if protocol and protocol.upper() != "PLAINTEXT":
        conf["security.protocol"] = protocol
    if mechanism:
        conf["sasl.mechanisms"] = mechanism
        conf["sasl.username"] = username or ""
        conf["sasl.password"] = password or ""
    return conf


class _TopicAdmin:
    """Thin adapter over confluent_kafka's AdminClient for topic CRUD.

    See the module-level note above for why this exists rather than
    ``scalo.kafka.admin.KafkaAdmin`` directly.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        admin_client_cls, _ = _admin_imports()
        self._admin = admin_client_cls(config)

    def list_topic_names(self, *, timeout: float = 10.0) -> set[str]:
        metadata = self._admin.list_topics(timeout=timeout)
        return set(metadata.topics.keys())

    def create(
        self, name: str, *, partitions: int, replication_factor: int, timeout: float = 30.0
    ) -> None:
        _, new_topic_cls = _admin_imports()
        new_topic = new_topic_cls(
            name, num_partitions=partitions, replication_factor=replication_factor
        )
        futures = self._admin.create_topics([new_topic], request_timeout=timeout)
        futures[name].result()

    def delete(self, name: str, *, timeout: float = 30.0) -> None:
        futures = self._admin.delete_topics([name], request_timeout=timeout)
        futures[name].result()


def _build_admin_client(
    *,
    bootstrap: str | None = None,
    provider: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> _TopicAdmin:
    return _TopicAdmin(
        _admin_config(bootstrap=bootstrap, provider=provider, username=username, password=password)
    )


def _derive_source_topics() -> list[str]:
    """Topics DFE's defined (enabled) sources need.

    Every source needs ``<source>_land`` (raw receiver landing); sources with
    a transform also need ``<source>_load`` (transformed output) - the
    ``_land``/``_load`` convention shared with scalo-rs, computed by
    ``Source.topic_land`` / ``Source.topic_load``
    (``dfe_engine.source.models``). Empty when ``DFE_SOURCES_DIR`` is unset or
    no sources are defined yet - callers fall back to requiring ``--topic``.
    """
    from dfe_engine.settings import get_settings
    from dfe_engine.source.registry import SourceRegistry

    sources_dir = get_settings().source.sources_dir
    if not sources_dir:
        return []

    registry = SourceRegistry(sources_directory=sources_dir, refresh_interval=0)
    try:
        sources = registry.get_all_sources(enabled_only=True)
    finally:
        registry.close()

    topics: list[str] = []
    for source in sources:
        topics.append(source.topic_land)
        if source.topic_load:
            topics.append(source.topic_load)
    return topics


def _admin_options(f):
    """The four broker/cred options every topics subcommand shares."""
    f = click.option(
        "--bootstrap", default=None, help="Bootstrap servers (else from settings/.env)."
    )(f)
    f = click.option(
        "--provider",
        default=None,
        help="Kafka provider for the credential contract (else DFE_KAFKA_PROVIDER).",
    )(f)
    f = click.option("--username", default=None, help="SASL username (else from settings/.env).")(f)
    f = click.option("--password", default=None, help="SASL password (else from settings/.env).")(f)
    return f


@kafka_group.group(name="topics", no_args_is_help=True)
def topics_group() -> None:
    """Explicit Kafka topic CRUD - create the topics DFE needs, never rely on
    broker ``auto.create.topics.enable``."""


@topics_group.command("list")
@click.option("--internal", is_flag=True, help="Include internal topics (e.g. __consumer_offsets).")
@_admin_options
def topics_list_cmd(
    internal: bool,
    bootstrap: str | None,
    provider: str | None,
    username: str | None,
    password: str | None,
) -> None:
    """List topics on the configured broker."""
    admin = _build_admin_client(
        bootstrap=bootstrap, provider=provider, username=username, password=password
    )
    names = sorted(admin.list_topic_names())
    if not internal:
        names = [n for n in names if not n.startswith("__")]
    if not names:
        click.echo("(no topics)")
        return
    for name in names:
        click.echo(name)


@topics_group.command("ensure")
@click.option(
    "--topic",
    "topics",
    multiple=True,
    metavar="NAME",
    help=(
        "Explicit topic (repeatable). Overrides the source-derived set - required "
        "when no sources are configured (source->topic mapping unavailable)."
    ),
)
@click.option(
    "--partitions", default=3, show_default=True, help="Partitions for newly created topics."
)
@click.option(
    "--replication-factor",
    default=1,
    show_default=True,
    help="Replication factor for newly created topics (raise for a multi-broker HA cluster).",
)
@click.option("--dry-run", is_flag=True, help="Show what would be created; create nothing.")
@_admin_options
def topics_ensure_cmd(
    topics: tuple[str, ...],
    partitions: int,
    replication_factor: int,
    dry_run: bool,
    bootstrap: str | None,
    provider: str | None,
    username: str | None,
    password: str | None,
) -> None:
    """Ensure the topics DFE's defined sources need exist, creating any that
    are missing (anti-"broker auto-create" - DFE decides partitions/RF, not
    the broker's first-produce default).

    Derives the topic set from the sources directory (``<source>_land``
    always, ``<source>_load`` when the source has a transform). Pass
    ``--topic`` (repeatable) to override when no sources are configured yet.
    """
    wanted = list(dict.fromkeys(topics)) if topics else _derive_source_topics()
    if not wanted:
        raise click.ClickException(
            "no topics to ensure: no sources are configured (DFE_SOURCES_DIR unset "
            "or empty), so the source->topic mapping is unavailable. Pass explicit "
            "--topic NAME (repeatable)."
        )

    admin = _build_admin_client(
        bootstrap=bootstrap, provider=provider, username=username, password=password
    )
    existing = admin.list_topic_names()
    missing = [t for t in wanted if t not in existing]
    already = [t for t in wanted if t in existing]

    for t in already:
        click.echo(f"exists       {t}")

    if not missing:
        click.echo("all topics already exist; nothing to create.")
        return

    if dry_run:
        for t in missing:
            click.echo(
                f"would create {t} (partitions={partitions}, replication_factor={replication_factor})"
            )
        return

    for t in missing:
        admin.create(t, partitions=partitions, replication_factor=replication_factor)
        click.echo(f"created      {t}")


@topics_group.command("delete")
@click.argument("topic")
@click.option("-y", "--yes", is_flag=True, help="Skip the confirm prompt.")
@_admin_options
def topics_delete_cmd(
    topic: str,
    yes: bool,
    bootstrap: str | None,
    provider: str | None,
    username: str | None,
    password: str | None,
) -> None:
    """Delete TOPIC. Destructive - the topic's data is gone, not recoverable."""
    if not yes:
        click.confirm(
            f"Delete Kafka topic {topic!r}? This is destructive and cannot be undone.",
            abort=True,
        )
    admin = _build_admin_client(
        bootstrap=bootstrap, provider=provider, username=username, password=password
    )
    admin.delete(topic)
    click.echo(f"deleted      {topic}")


# =============================================================================
# `dfe kafka lifecycle` - managed-cluster up/down/status (mounted scalo Typer)
# =============================================================================


def _mount_lifecycle() -> None:
    """Mount the managed-Kafka lifecycle Typer under ``dfe kafka lifecycle``.

    ``kafka_lifecycle_app`` (``cli/kafka_lifecycle.py``) is a scalo Typer, same
    as ``cli/ch_cloud.py``; ``typer.main.get_command`` bridges it to a click
    command so it slots straight under this click group - the same technique
    ``cli/auto/local.py`` uses to mount ``ch-cloud`` under ``dfe local``.
    Guarded so an import hiccup can never stop ``client-config``/``topics``
    loading.
    """
    try:
        import typer.main

        from dfe_engine.cli.kafka_lifecycle import kafka_lifecycle_app

        kafka_group.add_command(typer.main.get_command(kafka_lifecycle_app), name="lifecycle")
    except Exception:  # lifecycle is optional; never break the core group
        pass


_mount_lifecycle()


def attach_kafka(root: click.Group) -> None:
    """Mount the ``kafka`` group onto the root ``dfe`` command."""
    root.add_command(kafka_group)
