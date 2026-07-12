#  Project:      dfe-engine
#  File:         cli/kafka_lifecycle.py
#  Purpose:      `dfe kafka lifecycle` subcommands -- managed-Kafka cluster lifecycle
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""``dfe kafka lifecycle`` subcommands - managed-Kafka cluster lifecycle (WS-C,
dfe-engine#99).

A managed Kafka cluster (Confluent Cloud, provisioned MSK, Redpanda Cloud) has
NO pause - only deletion stops spend (see ``docs/MANAGED-KAFKA-LIFECYCLE.md``).
This group gives every managed provider the SAME three verbs behind one
interface (:class:`dfe_engine.kafka.cloud.base.ManagedKafkaProvider`)::

    dfe kafka lifecycle up     --provider redpanda-cloud   # create + mint creds + persist
    dfe kafka lifecycle status --provider redpanda-cloud   # $0/empty vs running
    dfe kafka lifecycle down   --provider redpanda-cloud   # DELETE-to-empty, never pause

``down`` ALWAYS tears down to provably empty: creds die BEFORE the cluster (an
orphaned key is a support ticket, not a saving), then the cluster, then it
asserts the provider's cluster list is empty. Mirrors ``cli/ch_cloud.py``'s
shape and reuses the SAME cred-persistence seam (``dfe_engine.kafka.cloud.
creds`` - a ``.env`` upsert + ``dfe_engine.secrets`` / scalo.secrets), per the
WS-C master plan's "build once, shared with the ClickHouse Cloud lifecycle"
note (docs/superpowers/plans/2026-07-12-kafka-contract-and-confluent-
lifecycle.md).

Redpanda Cloud Serverless is the first (proven) provider - promoted from the
``.tmp/redpanda_lifecycle.py`` scratch driver. Confluent Cloud slots into the
same ``ManagedKafkaProvider`` interface next (WS-C provider order); MSK is
gated on the AWS Layer 1 substrate (multi-cloud plan, unbuilt) and stubbed to
say so - neither makes a live call from this CLI today.
"""

from __future__ import annotations

import typer
from scalo.cli import Typer
from scalo.cli.output import print_error, print_info

from dfe_engine.kafka.cloud.base import ManagedKafkaProvider, ManagedKafkaProviderError

kafka_lifecycle_app = Typer(help="Managed-Kafka cluster lifecycle (up/down/status).")

KNOWN_PROVIDERS = ("redpanda-cloud", "confluent-cloud", "msk")


def build_provider(provider: str) -> ManagedKafkaProvider:
    """Resolve ``--provider`` to a :class:`ManagedKafkaProvider`.

    ``redpanda-cloud`` is the only built driver today. ``confluent-cloud`` and
    ``msk`` are named so the CLI's error is a clean "not yet", never an
    ``AttributeError``/``ImportError`` traceback, while the interface (and this
    dispatch point) is already shaped for them to slot in.
    """
    from dfe_engine.settings import load_settings

    if provider == "redpanda-cloud":
        from dfe_engine.kafka.cloud.redpanda import RedpandaCloudProvider

        return RedpandaCloudProvider(load_settings().kafka.redpanda_cloud)
    if provider == "confluent-cloud":
        raise ManagedKafkaProviderError(
            "confluent-cloud lifecycle is not yet built (WS-C provider order: "
            "redpanda-cloud first, confluent-cloud next) - see "
            "docs/superpowers/plans/2026-07-12-kafka-contract-and-confluent-"
            "lifecycle.md."
        )
    if provider == "msk":
        raise ManagedKafkaProviderError(
            "msk lifecycle is not yet built - AWS Layer 1 (multi-cloud plan) is "
            "unbuilt and MSK is gated on it."
        )
    raise ManagedKafkaProviderError(
        f"unknown provider {provider!r}; expected one of {KNOWN_PROVIDERS}"
    )


def _persist(provider: str, conn, *, env_path: str) -> None:
    from dfe_engine.kafka.cloud.creds import persist_connection
    from dfe_engine.settings import load_settings

    persist_connection(
        conn, provider=provider, secrets_settings=load_settings().secrets, env_path=env_path
    )


@kafka_lifecycle_app.command("up")
def lifecycle_up(
    provider: str = typer.Option("redpanda-cloud", "--provider", help="Managed Kafka provider."),
    env_file: str = typer.Option(
        ".env", "--env-file", help="Path to upsert the minted connection into."
    ),
) -> None:
    """Create the cluster if absent, mint data-plane creds, then persist them.

    Order is fixed: create/find the cluster, THEN mint creds, THEN persist -
    never the reverse (persisting before the mint succeeds would write stale
    or partial creds).
    """
    try:
        driver = build_provider(provider)
        conn = driver.up()
        _persist(provider, conn, env_path=env_file)
    except ManagedKafkaProviderError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    print_info(f"{provider}: up - cluster {conn.cluster_id} ({conn.bootstrap_servers})")


@kafka_lifecycle_app.command("down")
def lifecycle_down(
    provider: str = typer.Option("redpanda-cloud", "--provider", help="Managed Kafka provider."),
) -> None:
    """Teardown-to-empty: delete creds BEFORE the cluster, then assert empty.

    Never a pause - these providers have none; the only spend-stop is deletion
    (docs/MANAGED-KAFKA-LIFECYCLE.md). A cluster still present after this
    command is a defect, reported as a failure, not a convenience.
    """
    from dfe_engine.kafka.cloud.creds import forget_connection
    from dfe_engine.settings import load_settings

    try:
        driver = build_provider(provider)
        state = driver.down()
        forget_connection(provider=provider, secrets_settings=load_settings().secrets)
    except ManagedKafkaProviderError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    if state.is_empty:
        print_info(f"{provider}: teardown OK - cluster list empty ($0)")
        return
    print_error(f"{provider}: still present after teardown: {state.cluster_id} ({state.state})")
    raise typer.Exit(1)


@kafka_lifecycle_app.command("status")
def lifecycle_status(
    provider: str = typer.Option("redpanda-cloud", "--provider", help="Managed Kafka provider."),
) -> None:
    """Show $0/empty, or the running cluster's id/state/bootstrap."""
    try:
        state = build_provider(provider).status()
    except ManagedKafkaProviderError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    if state.is_empty:
        print_info(f"{provider}: no cluster ($0 / empty)")
        return
    print_info(f"{provider}: {state.cluster_id} ({state.state}) - {state.bootstrap_servers}")
