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
tools). ``dfe kafka lifecycle`` (mounted at the bottom of this module from
``cli/kafka_lifecycle.py``) is the managed-cluster up/down/status seam (WS-C,
dfe-engine#99) - see that module for detail.

``dfe kafka topics`` is NOT here: topic CRUD is the governed engine surface
(``api/v1/kafka_topics.py``, dfe-engine#97), so it is generated from the spec
like every other API family and carries that surface's RBAC and audit. The
commands here are the local ones that reach no engine at all.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile

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
    """Kafka provider tooling: emit client config, manage a managed cluster."""


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

    conn_cls, emit_properties, emit_kcat, emit_kafbat = _emitters()
    conn = conn_cls(
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
    """Mount the local ``kafka`` commands beside whatever the spec generated.

    ``dfe kafka topics`` comes from the API spec, so this must ADD to that group
    rather than replace it: a plain ``add_command`` would drop the whole governed
    topic surface on the floor. A name the generated group already holds is left
    alone - the governed route wins over a local duplicate.
    """
    generated = root.commands.get(kafka_group.name)
    if isinstance(generated, click.Group):
        for name, command in kafka_group.commands.items():
            generated.commands.setdefault(name, command)
        return
    root.add_command(kafka_group)
