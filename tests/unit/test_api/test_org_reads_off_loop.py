#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_org_reads_off_loop.py
#  Purpose:      A route that reads the org registry waits on it off the event loop
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A stalled org registry read holds up only the request waiting on it.

Each case stalls the read a route makes of the org registry, to check an org exists or to
find the orgs a caller is held to, then asks ``/livez`` for an answer while it is stalled.
A route that read on the event loop holds ``/livez`` up with it, as it would every request
and the probes. The org routes themselves are in ``test_orgs_off_loop``.
"""

from types import SimpleNamespace

import pytest

from dfe_engine.api.deps import get_clickhouse_client
from dfe_engine.secrets import build_secrets
from tests.support.held_callers import caller_in_group, define_role
from tests.support.stalled_call import Setup, Stall, registry_call, request_while_stalled
from tests.unit.test_api.test_apps import BASE, _deploy, _wire

ACCOUNTS = "/api/v1/auth/accounts"
KEYS = "/api/v1/auth/api-keys"
GROUPS = "/api/v1/auth/groups"
PASSWORD = "pw-alice-123456"

ADMIN = "admin"
TENANT = "tenant"
"""Who sends the request: the admin, or a caller held to the org acme."""

_LIST = registry_call("list")


class _Table:
    """A ClickHouse table a sample reads: DESCRIBE answers, and every read returns one row."""

    def query(self, sql, parameters=None, settings=None):
        if sql.startswith("DESCRIBE TABLE"):
            rows = [["_org_id", "String"], ["_json", "String"], ["timestamp_load", "String"]]
        else:
            rows = [['{"a": 1}']]
        return SimpleNamespace(result_rows=rows)


# What stalls, the request that waits on it, and who sends it.
_CASES: dict[str, tuple[Setup, str, str, dict | None, str]] = {
    "create an account": (
        _LIST,
        "POST",
        ACCOUNTS,
        {"username": "alice", "password": PASSWORD, "org": "acme"},
        ADMIN,
    ),
    "move an account to an org": (_LIST, "PUT", f"{ACCOUNTS}/viewer", {"org": "acme"}, ADMIN),
    "create an API key": (_LIST, "POST", KEYS, {"name": "pipeline", "org": "acme"}, ADMIN),
    "create a group in an org": (
        registry_call("get"),
        "POST",
        GROUPS,
        {"name": "acme-own", "roles": ["admin"], "scope": "org:acme"},
        ADMIN,
    ),
    "read the session": (_LIST, "GET", "/api/v1/auth/me", None, TENANT),
    "open the HyperDX connection": (_LIST, "GET", "/api/v1/hyperdx/connection", None, TENANT),
    "submit a sample": (
        _LIST,
        "POST",
        "/api/v1/sample",
        {"mode": "recent", "table": "`db`.`events`"},
        TENANT,
    ),
    "poll a sample": (_LIST, "GET", "/api/v1/samples/missing", None, TENANT),
    "list samples": (_LIST, "GET", "/api/v1/samples", None, TENANT),
}


@pytest.fixture
def backends(app, api_settings):
    """The ClickHouse table a sample reads, and the credential HyperDX's connection hands out."""
    app.dependency_overrides[get_clickhouse_client] = _Table
    build_secrets(api_settings.secrets).put("ch/orgs/acme", "acme-pw")
    yield
    app.dependency_overrides.pop(get_clickhouse_client, None)


@pytest.fixture
def tenant(app, client, api_settings, backends) -> dict[str, str]:
    """Headers of a caller held to the org acme, whose scoped role reads queries and samples."""
    app.state.org_registry.create("acme", org_ids=["t-acme"])
    define_role(app, "tenant_reader", ["query:execute", "sampler:read"], scoped=True)
    caller = caller_in_group(app, api_settings, "tenant", roles=["tenant_reader"], org_ids=["acme"])
    return {"Authorization": caller.headers["Authorization"]}


@pytest.mark.parametrize("case", list(_CASES))
def test_an_org_read_holds_up_only_its_own_request(
    case, client, app, admin_headers, tenant, monkeypatch
):
    setup, method, path, body, sender = _CASES[case]
    stall = Stall()
    setup(app, stall, monkeypatch)

    done = request_while_stalled(
        client,
        stall,
        case,
        method,
        path,
        json=body,
        headers=admin_headers if sender == ADMIN else tenant,
    )

    assert done.status_code < 500, done.text


def test_a_dry_run_reads_the_orgs_off_the_loop(
    client, app, api_settings, admin_headers, backends, tmp_path, monkeypatch
):
    _wire(app, tmp_path)
    _deploy(client, admin_headers)
    app.state.org_registry.create("acme", org_ids=["t-acme"])
    define_role(app, "dry-runner", ["dryrun:execute"], scoped=False)
    runner = caller_in_group(
        app, api_settings, "runner", roles=["dry-runner", "org_viewer"], org_ids=["acme"]
    )
    stall = Stall()
    _LIST(app, stall, monkeypatch)

    done = request_while_stalled(
        client,
        stall,
        "a dry run",
        "POST",
        f"{BASE}/files/transforms/dry-run",
        json={"name": "000.vrl", "content": ". = parse_json!(.message)\n"},
        headers={"Authorization": runner.headers["Authorization"]},
    )

    assert done.status_code < 500, done.text
