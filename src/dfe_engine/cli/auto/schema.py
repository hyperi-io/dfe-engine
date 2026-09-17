#  Project:      dfe-engine
#  File:         cli/auto/schema.py
#  Purpose:      `dfe schema` - plan, apply and read this deployment's schema
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""``dfe schema`` - the operator's view of the same phase the engine runs at boot.

Three commands, all against the local deployment's ClickHouse rather than the
API, so they work while the daemon is down:

  plan    render the pinned manifest and report what an apply would do, changing
          nothing
  apply   the boot phase, run by hand. It takes the SAME lease, so it serialises
          against a running engine rather than racing it
  status  the ledger: which dfe-schemas release each object on this cluster came
          from, and its checksum

``apply --allow-drift`` carries out the changes the boot phase refuses -- a
column type change and a TTL change. A changed ORDER BY or PARTITION BY stays
refused: ClickHouse has no operation for it and the table has to be rebuilt.
"""

from __future__ import annotations

import json
from typing import Any

import click


def _settings() -> Any:
    from dfe_engine.settings import load_settings

    return load_settings()


def _client(settings: Any) -> Any:
    from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
    from dfe_engine.settings import get_clickhouse_config

    return ClickHouseManager.get_instance(
        get_clickhouse_config(settings=settings)
    ).get_clickhouse_client()


@click.group("schema")
def schema_group() -> None:
    """Plan, apply and read the DFE ClickHouse schema."""


@schema_group.command("plan")
@click.option("--json", "as_json", is_flag=True, help="Emit the plan as JSON.")
def plan_cmd(as_json: bool) -> None:
    """Report what an apply would do. Changes nothing."""
    from dfe_engine.schema.phase import apply_plan
    from dfe_engine.schema.plan import build_plan

    settings = _settings()
    try:
        client = _client(settings)
        plan = build_plan(settings=settings, client=client)
        report = apply_plan(client, plan, dry_run=True)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        click.echo(
            json.dumps(
                {
                    "schemas_version": plan.schemas_version,
                    "topology": plan.topology,
                    "database": plan.data_database,
                    "counts": report.counts(),
                    "objects": [
                        {"object": o.qualified, "action": o.action, "drift": list(o.drift)}
                        for o in report.outcomes
                    ],
                    "overlay_refused": list(plan.refused),
                },
                indent=2,
            )
        )
        return
    click.echo(f"dfe-schemas {plan.schemas_version}, topology {plan.topology}")
    for outcome in report.outcomes:
        click.echo(outcome.describe())
    for refusal in plan.refused:
        click.echo(f"overlay refused {refusal}")
    click.echo(report.summary())


@schema_group.command("apply")
@click.option(
    "--allow-drift",
    is_flag=True,
    help="Also apply the changes the boot phase refuses: a column type, and a TTL.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the outcome as JSON.")
def apply_cmd(allow_drift: bool, as_json: bool) -> None:
    """Run the boot phase by hand, under the same lease. Exits 2 on a refusal."""
    from dfe_engine.schema.phase import run_bootstrap

    state = run_bootstrap(settings=_settings(), allow_drift=allow_drift)
    if as_json:
        click.echo(json.dumps(state.as_dict(), indent=2))
    else:
        click.echo(f"state {state.state}, dfe-schemas {state.schemas_version}")
        for count, value in state.counts.items():
            click.echo(f"  {count}: {value}")
        for refusal in state.refused:
            click.echo(f"refused {refusal}")
        if state.error:
            click.echo(f"error: {state.error}", err=True)
    if state.state == "failed":
        raise click.exceptions.Exit(1)
    if state.refused:
        raise click.exceptions.Exit(2)


@schema_group.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit the ledger as JSON.")
def status_cmd(as_json: bool) -> None:
    """Which dfe-schemas release each object on this cluster came from."""
    from dfe_engine.schema.ledger import MigrationLedger
    from dfe_engine.schema.plan import LEDGER_ID, build_plan

    settings = _settings()
    try:
        client = _client(settings)
        plan = build_plan(settings=settings, client=client)
        ledger_object = plan.by_id(LEDGER_ID)
        rows = MigrationLedger(
            client,
            database=ledger_object.database or plan.data_database,
            table=ledger_object.name,
        ).checksums()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    recorded = [
        {
            "object": f"{row.database}.{row.object}",
            "kind": row.kind,
            "schemas_version": row.schemas_version,
            "engine_version": row.engine_version,
            "checksum": row.checksum,
            "action": row.action,
            "topology": row.topology,
        }
        for row in sorted(rows.values(), key=lambda entry: (entry.database, entry.object))
    ]
    if as_json:
        click.echo(json.dumps(recorded, indent=2))
        return
    if not recorded:
        click.echo("the ledger is empty; the schema phase has not applied here")
        return
    for row in recorded:
        click.echo(
            f"{row['object']}  {row['kind']}  dfe-schemas {row['schemas_version']}  "
            f"{row['topology']}  {row['checksum'][:12]}"
        )


def attach_schema(root: click.Group) -> None:
    """Mount the ``schema`` group onto the root ``dfe`` command."""
    root.add_command(schema_group)
