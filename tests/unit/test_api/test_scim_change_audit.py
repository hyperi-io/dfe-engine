#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_scim_change_audit.py
#  Purpose:      Every SCIM user and group write emits one audit event
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""An IdP's SCIM writes leave one audit event each, marked as SCIM, and no secret.

Events are read from a real sink on the app's logger (the ``audit_events``
fixture), so what is asserted is what the deployment's log pipeline receives.
"""

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

BASE = "/api/v1/scim/v2"
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

ACCOUNT = "scim-audited"
GROUP = "scim-audited-group"


@dataclass(slots=True)
class Outcome:
    """What a write returned, and the values that must never reach its audit event."""

    status: int
    never_logged: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Case:
    domain: str
    change: str
    target_field: str
    target: str
    status: int
    run: Callable[..., Outcome]


def _account(app) -> None:
    app.state.account_store.create(ACCOUNT, secrets.token_urlsafe(16), groups=[])


def _hash(app, username: str) -> str:
    return app.state.account_store.get(username).password_hash


def _group(app, members: list[str] | None = None) -> None:
    app.state.group_store.create(GROUP, roles=["data_viewer"], members=members or [])


def _create_user(client, app, admin) -> Outcome:
    password = secrets.token_urlsafe(16)
    body = {"schemas": [USER_SCHEMA], "userName": ACCOUNT, "password": password}
    resp = client.post(f"{BASE}/Users", json=body, headers=admin)
    return Outcome(resp.status_code, [password, _hash(app, ACCOUNT)])


def _replace_user(client, app, admin) -> Outcome:
    _account(app)
    password = secrets.token_urlsafe(16)
    body = {
        "schemas": [USER_SCHEMA],
        "userName": ACCOUNT,
        "active": False,
        "externalId": "okta-77",
        "password": password,
    }
    resp = client.put(f"{BASE}/Users/{ACCOUNT}", json=body, headers=admin)
    return Outcome(resp.status_code, [password, _hash(app, ACCOUNT)])


def _deactivate_user(client, app, admin) -> Outcome:
    _account(app)
    body = {
        "schemas": [PATCH_SCHEMA],
        "Operations": [{"op": "replace", "path": "active", "value": False}],
    }
    resp = client.patch(f"{BASE}/Users/{ACCOUNT}", json=body, headers=admin)
    return Outcome(resp.status_code, [_hash(app, ACCOUNT)])


def _delete_user(client, app, admin) -> Outcome:
    _account(app)
    stored = _hash(app, ACCOUNT)
    resp = client.delete(f"{BASE}/Users/{ACCOUNT}", headers=admin)
    return Outcome(resp.status_code, [stored])


def _create_group(client, app, admin) -> Outcome:
    body = {
        "schemas": [GROUP_SCHEMA],
        "displayName": GROUP,
        "externalId": "grp-77",
        "members": [{"value": "viewer"}],
    }
    resp = client.post(f"{BASE}/Groups", json=body, headers=admin)
    return Outcome(resp.status_code)


def _replace_group(client, app, admin) -> Outcome:
    _group(app, members=["viewer"])
    body = {"schemas": [GROUP_SCHEMA], "displayName": GROUP, "members": [{"value": "operator"}]}
    resp = client.put(f"{BASE}/Groups/{GROUP}", json=body, headers=admin)
    return Outcome(resp.status_code)


def _patch_group(client, app, admin) -> Outcome:
    _group(app)
    body = {
        "schemas": [PATCH_SCHEMA],
        "Operations": [{"op": "add", "path": "members", "value": [{"value": "viewer"}]}],
    }
    resp = client.patch(f"{BASE}/Groups/{GROUP}", json=body, headers=admin)
    return Outcome(resp.status_code)


def _delete_group(client, app, admin) -> Outcome:
    _group(app, members=["viewer"])
    resp = client.delete(f"{BASE}/Groups/{GROUP}", headers=admin)
    return Outcome(resp.status_code)


CASES = {
    "user-create": Case("account", "created", "target_user", ACCOUNT, 201, _create_user),
    "user-replace": Case("account", "updated", "target_user", ACCOUNT, 200, _replace_user),
    "user-deactivate": Case("account", "updated", "target_user", ACCOUNT, 200, _deactivate_user),
    "user-delete": Case("account", "deleted", "target_user", ACCOUNT, 204, _delete_user),
    "group-create": Case("group", "created", "group_name", GROUP, 201, _create_group),
    "group-replace": Case("group", "updated", "group_name", GROUP, 200, _replace_group),
    "group-patch": Case("group", "updated", "group_name", GROUP, 200, _patch_group),
    "group-delete": Case("group", "deleted", "group_name", GROUP, 204, _delete_group),
}


@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
def test_a_scim_write_emits_one_audit_event_and_no_secret(
    client, app, admin_headers, audit_events, case
):
    outcome = case.run(client, app, admin_headers)

    assert outcome.status == case.status
    prefix = f"auth.{case.domain}."
    emitted = [event for event in audit_events if event["event"].startswith(prefix)]
    assert len(emitted) == 1, emitted
    event = emitted[0]
    assert event["event"] == f"{prefix}{case.change}"
    assert event["change"] == case.change
    assert event["admin_id"] == "admin"
    assert event[case.target_field] == case.target
    assert event["details"]["via"] == "scim"
    logged = json.dumps(event, default=str)
    leaked = [value for value in outcome.never_logged if value and value in logged]
    assert leaked == []


def test_a_deactivation_records_the_account_disabled(client, app, admin_headers, audit_events):
    _deactivate_user(client, app, admin_headers)

    (event,) = [e for e in audit_events if e["event"] == "auth.account.updated"]
    assert event["details"] == {"via": "scim", "fields": ["enabled"], "enabled": False}


def test_a_membership_replace_records_who_joined_and_who_left(
    client, app, admin_headers, audit_events
):
    _replace_group(client, app, admin_headers)

    (event,) = [e for e in audit_events if e["event"] == "auth.group.updated"]
    assert event["details"]["added"] == ["operator"]
    assert event["details"]["removed"] == ["viewer"]


def test_a_patch_that_changes_nothing_emits_no_event(client, app, admin_headers, audit_events):
    _account(app)
    body = {
        "schemas": [PATCH_SCHEMA],
        "Operations": [{"op": "replace", "path": "displayName", "value": "Someone"}],
    }

    resp = client.patch(f"{BASE}/Users/{ACCOUNT}", json=body, headers=admin_headers)

    assert resp.status_code == 200, resp.text
    assert [e for e in audit_events if e["event"].startswith("auth.account.")] == []


def test_a_refused_create_emits_no_event(client, app, admin_headers, audit_events):
    _account(app)
    body = {"schemas": [USER_SCHEMA], "userName": ACCOUNT}

    resp = client.post(f"{BASE}/Users", json=body, headers=admin_headers)

    assert resp.status_code == 409, resp.text
    assert [e for e in audit_events if e["event"].startswith("auth.account.")] == []
