#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_admin_change_audit.py
#  Purpose:      Every account, group, API key and role write emits one audit event
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Admin writes to identities and grants leave one audit event each, and no secret.

Events are read from a real sink on the app's logger (the ``audit_events``
fixture), so what is asserted is what the deployment's log pipeline receives.
"""

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

ACCOUNT = "audited"
GROUP = "audited-group"
KEY = "audited-key"
ROLE = "audited_role"
VIEWER_PASSWORD = "test-viewer-pw"


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
    actor: str = "admin"


def _account(app) -> None:
    app.state.account_store.create(ACCOUNT, secrets.token_urlsafe(16), groups=[])


def _hash(app, username: str) -> str:
    return app.state.account_store.get(username).password_hash


def _group(app, members: list[str] | None = None) -> None:
    app.state.group_store.create(GROUP, roles=["data_viewer"], members=members or [])


def _create_account(client, app, admin, viewer) -> Outcome:
    password = secrets.token_urlsafe(16)
    body = {"username": ACCOUNT, "password": password, "groups": ["dfe-viewers"]}
    resp = client.post("/api/v1/auth/accounts", json=body, headers=admin)
    return Outcome(resp.status_code, [password, _hash(app, ACCOUNT)])


def _update_account(client, app, admin, viewer) -> Outcome:
    _account(app)
    phone = "+61 2 5550 0199"
    body = {"blocked": True, "phone": phone}
    resp = client.put(f"/api/v1/auth/accounts/{ACCOUNT}", json=body, headers=admin)
    return Outcome(resp.status_code, [phone, _hash(app, ACCOUNT)])


def _update_own_account(client, app, admin, viewer) -> Outcome:
    phone = "+61 2 5550 0177"
    resp = client.put("/api/v1/auth/accounts/me", json={"phone": phone}, headers=viewer)
    return Outcome(resp.status_code, [phone, _hash(app, "viewer")])


def _change_own_password(client, app, admin, viewer) -> Outcome:
    new = secrets.token_urlsafe(16)
    body = {"current_password": VIEWER_PASSWORD, "new_password": new}
    resp = client.post("/api/v1/auth/accounts/reset-password", json=body, headers=viewer)
    return Outcome(resp.status_code, [VIEWER_PASSWORD, new, _hash(app, "viewer")])


def _reset_password(client, app, admin, viewer) -> Outcome:
    _account(app)
    new = secrets.token_urlsafe(16)
    url = f"/api/v1/auth/accounts/{ACCOUNT}/reset-password"
    resp = client.post(url, json={"new_password": new}, headers=admin)
    return Outcome(resp.status_code, [new, _hash(app, ACCOUNT)])


def _delete_account(client, app, admin, viewer) -> Outcome:
    _account(app)
    stored = _hash(app, ACCOUNT)
    resp = client.delete(f"/api/v1/auth/accounts/{ACCOUNT}", headers=admin)
    return Outcome(resp.status_code, [stored])


def _account_attributes(client, app, admin, viewer) -> Outcome:
    _account(app)
    body = {"attributes": {"team": "blue"}}
    resp = client.put(f"/api/v1/auth/accounts/{ACCOUNT}/attributes", json=body, headers=admin)
    return Outcome(resp.status_code)


def _account_sensitive_attributes(client, app, admin, viewer) -> Outcome:
    _account(app)
    value = secrets.token_hex(12)
    body = {"attributes": {"pager_pin": value}}
    url = f"/api/v1/auth/accounts/{ACCOUNT}/sensitive-attributes"
    resp = client.put(url, json=body, headers=admin)
    return Outcome(resp.status_code, [value])


def _create_group(client, app, admin, viewer) -> Outcome:
    body = {"name": GROUP, "roles": ["data_viewer"], "members": ["viewer"]}
    resp = client.post("/api/v1/auth/groups", json=body, headers=admin)
    return Outcome(resp.status_code)


def _update_group(client, app, admin, viewer) -> Outcome:
    _group(app)
    body = {"roles": ["data_viewer", "data_analyst_viewer"], "members": ["viewer"]}
    resp = client.put(f"/api/v1/auth/groups/{GROUP}", json=body, headers=admin)
    return Outcome(resp.status_code)


def _add_member(client, app, admin, viewer) -> Outcome:
    _group(app)
    body = {"username": "viewer"}
    resp = client.post(f"/api/v1/auth/groups/{GROUP}/members", json=body, headers=admin)
    return Outcome(resp.status_code)


def _remove_member(client, app, admin, viewer) -> Outcome:
    _group(app, members=["viewer"])
    resp = client.delete(f"/api/v1/auth/groups/{GROUP}/members/viewer", headers=admin)
    return Outcome(resp.status_code)


def _delete_group(client, app, admin, viewer) -> Outcome:
    _group(app)
    resp = client.delete(f"/api/v1/auth/groups/{GROUP}", headers=admin)
    return Outcome(resp.status_code)


def _group_attributes(client, app, admin, viewer) -> Outcome:
    _group(app)
    body = {"attributes": {"team": "blue"}}
    resp = client.put(f"/api/v1/auth/groups/{GROUP}/attributes", json=body, headers=admin)
    return Outcome(resp.status_code)


def _group_sensitive_attributes(client, app, admin, viewer) -> Outcome:
    _group(app)
    value = secrets.token_hex(12)
    body = {"attributes": {"pager_pin": value}}
    url = f"/api/v1/auth/groups/{GROUP}/sensitive-attributes"
    resp = client.put(url, json=body, headers=admin)
    return Outcome(resp.status_code, [value])


def _create_api_key(client, app, admin, viewer) -> Outcome:
    resp = client.post("/api/v1/auth/api-keys", json={"name": KEY}, headers=admin)
    full_key = resp.json()["full_key"]
    long_token = full_key.rsplit("_", 1)[1]
    stored = app.state.api_key_store.get(KEY).key_hash
    return Outcome(resp.status_code, [full_key, long_token, stored])


def _revoke_api_key(client, app, admin, viewer) -> Outcome:
    meta, full_key = app.state.api_key_store.create(KEY)
    long_token = full_key.rsplit("_", 1)[1]
    resp = client.delete(f"/api/v1/auth/api-keys/{meta.short_token}", headers=admin)
    return Outcome(resp.status_code, [full_key, long_token, meta.key_hash])


def _create_role(client, app, admin, viewer) -> Outcome:
    body = {"name": ROLE, "permissions": ["query:execute"]}
    resp = client.post("/api/v1/auth/roles", json=body, headers=admin)
    return Outcome(resp.status_code)


def _update_role(client, app, admin, viewer) -> Outcome:
    app.state.role_store.create(ROLE, description="", permissions=["query:execute"])
    body = {"permissions": ["query:read"]}
    resp = client.put(f"/api/v1/auth/roles/{ROLE}", json=body, headers=admin)
    return Outcome(resp.status_code)


def _delete_role(client, app, admin, viewer) -> Outcome:
    app.state.role_store.create(ROLE, description="", permissions=["query:execute"])
    resp = client.delete(f"/api/v1/auth/roles/{ROLE}", headers=admin)
    return Outcome(resp.status_code)


CASES = {
    "account-create": Case("account", "created", "target_user", ACCOUNT, 201, _create_account),
    "account-update": Case("account", "updated", "target_user", ACCOUNT, 200, _update_account),
    "account-update-own": Case(
        "account", "updated", "target_user", "viewer", 200, _update_own_account, actor="viewer"
    ),
    "account-change-own-password": Case(
        "account",
        "password_changed",
        "target_user",
        "viewer",
        200,
        _change_own_password,
        actor="viewer",
    ),
    "account-reset-password": Case(
        "account", "password_reset", "target_user", ACCOUNT, 200, _reset_password
    ),
    "account-delete": Case("account", "deleted", "target_user", ACCOUNT, 204, _delete_account),
    "account-attributes": Case(
        "account", "attributes_updated", "target_user", ACCOUNT, 200, _account_attributes
    ),
    "account-sensitive-attributes": Case(
        "account",
        "sensitive_attributes_updated",
        "target_user",
        ACCOUNT,
        200,
        _account_sensitive_attributes,
    ),
    "group-create": Case("group", "created", "group_name", GROUP, 201, _create_group),
    "group-update": Case("group", "updated", "group_name", GROUP, 200, _update_group),
    "group-add-member": Case("group", "member_added", "group_name", GROUP, 200, _add_member),
    "group-remove-member": Case(
        "group", "member_removed", "group_name", GROUP, 200, _remove_member
    ),
    "group-delete": Case("group", "deleted", "group_name", GROUP, 204, _delete_group),
    "group-attributes": Case(
        "group", "attributes_updated", "group_name", GROUP, 200, _group_attributes
    ),
    "group-sensitive-attributes": Case(
        "group",
        "sensitive_attributes_updated",
        "group_name",
        GROUP,
        200,
        _group_sensitive_attributes,
    ),
    "api-key-create": Case("api_key", "created", "key_name", KEY, 201, _create_api_key),
    "api-key-revoke": Case("api_key", "revoked", "key_name", KEY, 204, _revoke_api_key),
    "role-create": Case("role", "created", "role_name", ROLE, 201, _create_role),
    "role-update": Case("role", "updated", "role_name", ROLE, 200, _update_role),
    "role-delete": Case("role", "deleted", "role_name", ROLE, 204, _delete_role),
}


@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
def test_a_write_emits_one_audit_event_and_no_secret(
    client, app, admin_headers, viewer_headers, audit_events, case
):
    outcome = case.run(client, app, admin_headers, viewer_headers)

    assert outcome.status == case.status
    prefix = f"auth.{case.domain}."
    emitted = [event for event in audit_events if event["event"].startswith(prefix)]
    assert len(emitted) == 1, emitted
    event = emitted[0]
    assert event["event"] == f"{prefix}{case.change}"
    assert event["change"] == case.change
    assert event["admin_id"] == case.actor
    assert event[case.target_field] == case.target
    logged = json.dumps(event, default=str)
    leaked = [value for value in outcome.never_logged if value and value in logged]
    assert leaked == []


def test_a_refused_write_emits_no_change_event(client, admin_headers, audit_events):
    resp = client.delete("/api/v1/auth/roles/infra_viewer", headers=admin_headers)

    assert resp.status_code == 409
    assert [e for e in audit_events if e["event"].startswith("auth.role.")] == []


def test_a_group_role_change_records_the_roles_granted(client, app, admin_headers, audit_events):
    _group(app)
    roles = ["data_viewer", "data_analyst_viewer"]

    client.put(f"/api/v1/auth/groups/{GROUP}", json={"roles": roles}, headers=admin_headers)

    (event,) = [e for e in audit_events if e["event"] == "auth.group.updated"]
    assert event["details"]["roles"] == roles
    assert event["details"]["fields"] == ["roles"]


def test_a_role_write_records_what_the_role_now_grants(client, admin_headers, audit_events):
    body = {"name": ROLE, "permissions": ["query:execute"], "scoped": True}

    client.post("/api/v1/auth/roles", json=body, headers=admin_headers)

    (event,) = [e for e in audit_events if e["event"] == "auth.role.created"]
    assert event["details"] == {"permissions": ["query:execute"], "scoped": True}
