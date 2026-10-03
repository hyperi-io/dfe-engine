#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_dry_run_default_install.py
#  Purpose:      A transform dry run samples rows on a default install, held to the caller's orgs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The dry run samples through the real sampler without logreducer.

logreducer is not a dependency, so the sampler's gated default mode cannot run in
a default install. The dry run asks for the newest rows instead, and a caller with
no platform grant still has that read bound to its org's tenant ids.
"""

import pytest

from dfe_engine.api.deps import get_clickhouse_client
from tests.support.held_callers import caller_in_group, define_role
from tests.unit.test_api.test_apps import BASE, _deploy, _wire

ACME_TENANTS = ["t-acme-1", "t-acme-2"]
HELD = "_org_id IN {orgs:Array(String)}"


class _Result:
    def __init__(self, rows: list[list[str]]) -> None:
        self.result_rows = rows


class _Table:
    """A table carrying ``_org_id``: DESCRIBE answers, and every row read is recorded."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, dict]] = []

    def query(self, sql, parameters=None, settings=None):
        if sql.startswith("DESCRIBE TABLE"):
            return _Result(
                [["_org_id", "String"], ["_json", "String"], ["timestamp_load", "String"]]
            )
        self.reads.append((sql, parameters or {}))
        return _Result([['{"message": "{\\"a\\": 1}"}']])


@pytest.fixture
def table(app):
    ch = _Table()
    app.dependency_overrides[get_clickhouse_client] = lambda: ch
    yield ch
    app.dependency_overrides.pop(get_clickhouse_client, None)


@pytest.fixture
def deployed(app, client, admin_headers, tmp_path):
    """A deployed transform instance, the org acme, and a runner role that is no platform grant."""
    _wire(app, tmp_path)
    _deploy(client, admin_headers)
    app.state.org_registry.create("acme", org_ids=ACME_TENANTS)
    define_role(app, "dry-runner", ["dryrun:execute"], scoped=False)


def _dry_run(client, headers=None):
    return client.post(
        f"{BASE}/files/transforms/dry-run",
        json={"name": "000.vrl", "content": ". = parse_json!(.message)\n"},
        headers=headers,
    )


def test_a_held_dry_run_samples_the_newest_rows_of_its_orgs(app, api_settings, deployed, table):
    runner = caller_in_group(
        app, api_settings, "runner", roles=["dry-runner", "org_viewer"], org_ids=["acme"]
    )

    resp = _dry_run(runner)

    assert resp.status_code == 200, resp.text
    assert resp.json()["sampled"] == 1
    [(sql, params)] = table.reads
    assert f"WHERE {HELD}" in sql
    assert "DESC LIMIT" in sql
    assert params["orgs"] == ACME_TENANTS


def test_a_platform_dry_run_samples_the_newest_rows_of_every_org(
    client, admin_headers, deployed, table
):
    resp = _dry_run(client, admin_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["sampled"] == 1
    [(sql, params)] = table.reads
    assert "_org_id" not in sql
    assert "orgs" not in params
