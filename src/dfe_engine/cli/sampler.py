#  Project:      dfe-engine
#  File:         cli/sampler.py
#  Purpose:      `dfe-api sample` subcommands -- pull sample data from a source
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""``dfe-api sample`` subcommands.

Pull a small, inspectable slice of a source's events from ClickHouse (landed
``_json``) or Kafka, in one of four modes: recent, random, smart (default,
logreducer representative sample) and anomaly (logreducer outliers). Talks to the
same ``Sampler`` service the API uses - no HTTP hop.
"""

from __future__ import annotations

import asyncio
import json

import typer
from scalo.cli import Typer
from scalo.cli.output import print_error, print_info, print_table

from dfe_engine.sampling import SampleBackend, SampleMode, Sampler, SampleRequest, SamplerError
from dfe_engine.settings import load_settings

sampler_app = Typer(help="Pull sample data from a source (ClickHouse or Kafka).")


def _clickhouse_client(settings):
    from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
    from dfe_engine.settings import get_clickhouse_config

    return ClickHouseManager.get_instance(get_clickhouse_config(settings)).get_clickhouse_client()


def _source_registry(settings):
    if not settings.source.sources_dir:
        return None
    from dfe_engine.source.registry import SourceRegistry

    return SourceRegistry(sources_directory=settings.source.sources_dir)


@sampler_app.command("get")
def sample_get(
    source: str = typer.Argument(
        "", help="Registered source name (or use --table/--topic for ad-hoc)."
    ),
    mode: SampleMode | None = typer.Option(
        None, help="recent|random|smart|anomaly (default: sampler.default_mode)."
    ),
    backend: SampleBackend = typer.Option(SampleBackend.CLICKHOUSE, help="clickhouse|kafka."),
    limit: int = typer.Option(0, "--limit", "-n", help="Rows to return (0 = config default)."),
    table: str = typer.Option("", help="Explicit ClickHouse table (ad-hoc)."),
    topic: str = typer.Option("", help="Explicit Kafka topic (ad-hoc)."),
    filter_sql: str = typer.Option("", "--filter", help="Trusted SQL WHERE predicate (CH only)."),
    since: str = typer.Option("", help="Lower time bound (ISO 8601)."),
    until: str = typer.Option("", help="Upper time bound (ISO 8601)."),
    seed: int = typer.Option(-1, help="Seed for random mode (-1 = unseeded)."),
    output: str = typer.Option("raw", "--output", "-o", help="raw|json|table."),
) -> None:
    """Pull a sample from SOURCE (or an explicit --table/--topic)."""
    settings = load_settings()
    req = SampleRequest(
        # Fall back to the documented operator default (sampler.default_mode) when
        # --mode is not given, instead of a hardcoded SMART.
        mode=mode or SampleMode(settings.sampler.default_mode),
        backend=backend,
        limit=limit or None,
        source=source or None,
        table=table or None,
        topic=topic or None,
        filter=filter_sql or None,
        since=since or None,
        until=until or None,
        seed=None if seed < 0 else seed,
    )
    sampler = Sampler(settings.sampler, settings.kafka, settings.clickhouse)
    registry = _source_registry(settings)
    ch = _clickhouse_client(settings) if backend == SampleBackend.CLICKHOUSE else None

    try:
        result = asyncio.run(sampler.run(req, ch, registry))
    except SamplerError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    _emit(result, output)


@sampler_app.command("modes")
def sample_modes() -> None:
    """List the available sample modes."""
    rows = [
        ["recent", "Newest rows first (fast tail)."],
        ["random", "Uniform-ish random rows (fair distribution)."],
        ["smart", "logreducer representative/diverse sample (default)."],
        ["anomaly", "logreducer isolation-forest outliers (the weird events)."],
    ]
    print_table(rows, title="Sample modes", headers=["mode", "description"])


def _emit(result: dict, output: str) -> None:
    if output == "json":
        print(json.dumps(result, indent=2, default=str))
        return
    if output == "table":
        note = f" - {result['note']}" if result.get("note") else ""
        print_info(
            f"{result['count']} rows from {result['target']} "
            f"({result['mode']}/{result['backend']}){note}"
        )
        keys = result.get("keys") or []
        print_table([[k] for k in keys], title="Discovered keys", headers=["key"])
        return
    # raw (default): one event per line, ready to pipe
    for line in result.get("lines", []):
        print(line)


def register_sampler_commands(app: Typer) -> None:
    """Register the ``sample`` subcommand group on *app*."""
    app.add_typer(sampler_app, name="sample")
