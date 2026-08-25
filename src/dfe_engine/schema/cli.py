#  Project:      dfe-engine
#  File:         schema/cli.py
#  Purpose:      dfe-schema - deploy the DFE ClickHouse schema without the daemon
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""`dfe-schema` -- stand the DFE ClickHouse schema up from a one-shot process.

The ``dfe`` CLI cannot do this: it is generated from the OpenAPI spec, so it is a
client by construction and needs the daemon it is meant to precede. This entry
point calls the same schema code directly -- no HTTP, no FastAPI, no daemon --
which is what lets dfe-infra and dfe-docker pre-deploy the schema from the same
image the engine ships in. It is the fourth standalone binary in the engine
image, alongside ``dfe-hunt-runner`` and ``dfe-keda-shim``.

Two commands:

  apply -- create or reconcile every core table and report what changed. This is
      the gate: a failure exits non-zero so the ArgoCD wave blocks and the
      compose dependency never reports ``service_completed_successfully``.

  check -- report drift and change nothing, exiting 2 when the live schema is not
      what the code says it should be. For a readiness probe or a smoke test.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

import clickhouse_connect
import typer

from dfe_engine.schema.applier import ApplyReport, SchemaApplyError
from dfe_engine.schema.core_schema import (
    CoreSchemaTargets,
    apply_core_schema,
    apply_query_log_archive,
)
from dfe_engine.settings import ClickHouseSettings, load_clickhouse_settings

app = typer.Typer(help="dfe-schema: deploy the DFE ClickHouse schema.", no_args_is_help=False)

_CONNECT_RETRY_SECONDS = 3.0


def _client(ch: ClickHouseSettings) -> Any:
    """A raw clickhouse-connect client from settings.

    Raw rather than the pooled ``ClickHouseManager``: this process does one pass
    and exits, so the resilience layer's reconnect loop would only delay the
    failure a gate is meant to surface.
    """
    params: dict[str, Any] = {
        "host": ch.host,
        "port": ch.port,
        "username": ch.username,
        "password": ch.password,
        "secure": ch.secure,
        "verify": ch.verify,
    }
    if ch.ca_cert:
        params["ca_cert"] = ch.ca_cert
    return clickhouse_connect.get_client(**params)


def _connect(ch: ClickHouseSettings, wait: float) -> Any:
    """Connect, retrying for up to *wait* seconds.

    A schema job starts the moment its wave does, which on a fresh deploy is
    while ClickHouse is still electing. Retrying here is what stops the job
    failing on a cluster that is merely not up yet.
    """
    deadline = time.monotonic() + max(wait, 0.0)
    last: Exception | None = None
    while True:
        try:
            return _client(ch)
        except Exception as exc:
            last = exc
            if time.monotonic() >= deadline:
                break
            typer.echo(f"waiting for ClickHouse: {exc}", err=True)
            time.sleep(_CONNECT_RETRY_SECONDS)
    raise SchemaApplyError(f"could not reach ClickHouse: {last}")


def _emit(report: ApplyReport, targets: CoreSchemaTargets, *, as_json: bool) -> None:
    """Print the report, as JSON or as one line per object."""
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "database": targets.database,
                    "profile": targets.profile,
                    "changed": report.changed,
                    "databases_created": report.databases_created,
                    "tables": [
                        {
                            "database": change.database,
                            "table": change.table,
                            "action": change.action,
                            "columns_added": list(change.columns_added),
                            "engine": change.engine,
                            "on_cluster": change.on_cluster.strip(),
                        }
                        for change in report.tables
                    ],
                    "statements": report.statements,
                },
                indent=2,
            )
        )
        return
    for line in report.lines():
        typer.echo(line)
    typer.echo(report.summary())


@app.command("apply")
def apply(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the statements that would run; change nothing."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the report as JSON."),
    wait: float = typer.Option(
        0.0, help="Seconds to keep retrying the ClickHouse connection before failing."
    ),
    query_log_archive: bool = typer.Option(
        True,
        "--query-log-archive/--no-query-log-archive",
        help="Also stand up the query-log cost archive (never fatal).",
    ),
) -> None:
    """Create or reconcile every core table, and say what changed."""
    ch = load_clickhouse_settings()
    targets = CoreSchemaTargets.from_clickhouse(ch)
    try:
        client = _connect(ch, wait)
        report = apply_core_schema(
            client,
            targets,
            topology_setting=ch.topology,
            dry_run=dry_run,
        )
    except SchemaApplyError as exc:
        typer.echo(f"schema apply failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if query_log_archive and not dry_run:
        apply_query_log_archive(client, targets)

    _emit(report, targets, as_json=as_json)


@app.command("check")
def check(
    as_json: bool = typer.Option(False, "--json", help="Emit the report as JSON."),
    wait: float = typer.Option(
        0.0, help="Seconds to keep retrying the ClickHouse connection before failing."
    ),
) -> None:
    """Report schema drift without changing anything. Exits 2 when it finds any."""
    ch = load_clickhouse_settings()
    targets = CoreSchemaTargets.from_clickhouse(ch)
    try:
        client = _connect(ch, wait)
        report = apply_core_schema(
            client,
            targets,
            topology_setting=ch.topology,
            dry_run=True,
        )
    except SchemaApplyError as exc:
        typer.echo(f"schema check failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    _emit(report, targets, as_json=as_json)
    if report.changed:
        raise typer.Exit(code=2)


def main() -> None:
    """Entry point. Bare ``dfe-schema`` applies -- the common case in a job spec."""
    if len(sys.argv) == 1:
        sys.argv.append("apply")
    app()


if __name__ == "__main__":
    main()
