#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_account_reauth.py
#  Purpose:      API tests for the re-auth grant every access change needs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dfe_engine.api.deps import jwt_authority_for
from dfe_engine.auth.bootstrap import admin_account_name
from dfe_engine.auth.identity_links import LINK_TICKET_AUDIENCE
from dfe_engine.auth.reauth import REAUTH_AUDIENCE, REAUTH_HEADER
from dfe_engine.settings import DFESettings
from tests.support.accounts import session_token
from tests.support.reauth import REAUTH_CHECKS
from tests.unit.test_api.conftest import ADMIN_PASSWORD

ACCOUNTS = "/api/v1/auth/accounts"
API_KEYS = "/api/v1/auth/api-keys"
GROUPS = "/api/v1/auth/groups"
PROVIDERS = "/api/v1/auth/oidc-providers"
REAUTH = "/api/v1/auth/reauth"
RETIRE = "/api/v1/auth/setup/retire-admin"
ROLES = "/api/v1/auth/roles"
SCIM = "/api/v1/scim/v2"
PASSWORD = "reauth-password-1"
SCIM_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"


def _account_body(username: str, **fields: object) -> dict[str, object]:
    """A create-account body for a local account called *username*."""
    return {
        "email": f"{username}@example.test",
        "groups": ["dfe-viewers"],
        "name": username,
        "password": PASSWORD,
        "username": username,
        **fields,
    }


def _provider_body(name: str) -> dict[str, str]:
    """A create-provider body for a generic provider called *name*."""
    return {
        "client_id_env": "OIDC_CLIENT_ID",
        "display_name": name,
        "issuer": "https://accounts.example.com",
        "name": name,
        "type": "generic",
    }


# Every access change, as (method, path, body). ``{short_token}`` is bob-key's.
# Writes each route refuses for a reason of its own, which it answers before asking for the grant, as (method, path, body, status).
REFUSED_FIRST = [
    pytest.param("POST", ACCOUNTS, _account_body("bob"), 409, id="account-create-taken"),
    pytest.param("PUT", f"{ACCOUNTS}/bob", {"org": "no-such-org"}, 422, id="account-unknown-org"),
    pytest.param("POST", GROUPS, {"name": "bob-team", "roles": []}, 409, id="group-create-taken"),
    pytest.param(
        "POST",
        GROUPS,
        {"name": "half-linked", "roles": [], "source_id": "abc"},
        422,
        id="group-create-id-without-provider",
    ),
    pytest.param(
        "PUT", f"{GROUPS}/bob-team", {"roles": ["org_admin"]}, 422, id="group-system-org-admin"
    ),
    pytest.param(
        "PUT",
        f"{ROLES}/bob-role",
        {"permissions": ["group:write"], "scoped": True},
        422,
        id="role-scoped-write",
    ),
    pytest.param("POST", PROVIDERS, _provider_body("bob-idp"), 409, id="provider-create-taken"),
    pytest.param(
        "PUT", f"{PROVIDERS}/bob-idp", {"org": "no-such-org"}, 422, id="provider-unknown-org"
    ),
    pytest.param("POST", API_KEYS, {"name": "bob-key"}, 409, id="api-key-create-taken"),
]

WRITES = [
    pytest.param("POST", ACCOUNTS, _account_body("dave"), id="account-create"),
    pytest.param("PUT", f"{ACCOUNTS}/bob", {"name": "Bobby"}, id="account-update"),
    pytest.param(
        "POST",
        f"{ACCOUNTS}/bob/reset-password",
        {"new_password": "reset-password-12"},
        id="account-reset",
    ),
    pytest.param("DELETE", f"{ACCOUNTS}/bob", None, id="account-delete"),
    pytest.param("DELETE", f"{ACCOUNTS}/bob/identities/bob-sub", None, id="account-unlink"),
    pytest.param(
        "POST",
        f"{ACCOUNTS}/bob/rotate-password",
        {"new_password": "rotated-password-1234567"},
        id="account-rotate",
    ),
    pytest.param("PUT", f"{ACCOUNTS}/bob/attributes", {"attributes": {}}, id="account-attributes"),
    pytest.param(
        "PUT",
        f"{ACCOUNTS}/bob/sensitive-attributes",
        {"attributes": {}},
        id="account-sensitive-attributes",
    ),
    pytest.param("POST", GROUPS, {"name": "new-team", "roles": []}, id="group-create"),
    pytest.param("PUT", f"{GROUPS}/bob-team", {"description": "Bob's"}, id="group-update"),
    pytest.param(
        "POST", f"{GROUPS}/bob-team/members", {"username": "carol"}, id="group-add-member"
    ),
    pytest.param("DELETE", f"{GROUPS}/bob-team/members/bob", None, id="group-remove-member"),
    pytest.param("DELETE", f"{GROUPS}/bob-team", None, id="group-delete"),
    pytest.param("PUT", f"{GROUPS}/bob-team/attributes", {"attributes": {}}, id="group-attributes"),
    pytest.param(
        "PUT",
        f"{GROUPS}/bob-team/sensitive-attributes",
        {"attributes": {}},
        id="group-sensitive-attributes",
    ),
    pytest.param("PUT", f"{ROLES}/bob-role", {"permissions": ["group:read"]}, id="role-update"),
    pytest.param("DELETE", f"{ROLES}/bob-role", None, id="role-delete"),
    pytest.param("POST", PROVIDERS, _provider_body("new-idp"), id="provider-create"),
    pytest.param("PUT", f"{PROVIDERS}/bob-idp", {"display_name": "Bob"}, id="provider-update"),
    pytest.param("DELETE", f"{PROVIDERS}/bob-idp", None, id="provider-delete"),
    pytest.param("POST", f"{PROVIDERS}/bob-idp/sync", None, id="provider-sync"),
    pytest.param("POST", API_KEYS, {"name": "new-key"}, id="api-key-create"),
    pytest.param("DELETE", f"{API_KEYS}/{{short_token}}", None, id="api-key-revoke"),
    pytest.param(
        "POST", f"{SCIM}/Users", {"schemas": [SCIM_USER], "userName": "erin"}, id="scim-user-create"
    ),
    pytest.param("PUT", f"{SCIM}/Users/bob", {"schemas": [SCIM_USER]}, id="scim-user-replace"),
    pytest.param("PATCH", f"{SCIM}/Users/bob", {"Operations": []}, id="scim-user-patch"),
    pytest.param("DELETE", f"{SCIM}/Users/bob", None, id="scim-user-delete"),
    pytest.param(
        "POST",
        f"{SCIM}/Groups",
        {"displayName": "scim-team", "schemas": [SCIM_GROUP]},
        id="scim-group-create",
    ),
    pytest.param(
        "PUT", f"{SCIM}/Groups/bob-team", {"schemas": [SCIM_GROUP]}, id="scim-group-replace"
    ),
    pytest.param("PATCH", f"{SCIM}/Groups/bob-team", {"Operations": []}, id="scim-group-patch"),
    pytest.param("DELETE", f"{SCIM}/Groups/bob-team", None, id="scim-group-delete"),
]

# A change of each kind, as (kind, target, method, path, body).
OPENED = [
    pytest.param("account", "dave", "POST", ACCOUNTS, _account_body("dave"), id="account-create"),
    pytest.param("account", "bob", "PUT", f"{ACCOUNTS}/bob", {"name": "Bobby"}, id="account"),
    pytest.param(
        "group", "bob-team", "PUT", f"{GROUPS}/bob-team", {"description": "B"}, id="group"
    ),
    pytest.param("role", "bob-role", "DELETE", f"{ROLES}/bob-role", None, id="role"),
    pytest.param(
        "provider", "new-idp", "POST", PROVIDERS, _provider_body("new-idp"), id="provider-create"
    ),
    pytest.param("provider", "bob-idp", "DELETE", f"{PROVIDERS}/bob-idp", None, id="provider"),
    pytest.param("api_key", "new-key", "POST", API_KEYS, {"name": "new-key"}, id="api-key-create"),
    pytest.param(
        "api_key", "{short_token}", "DELETE", f"{API_KEYS}/{{short_token}}", None, id="api-key"
    ),
    pytest.param(
        "account",
        "erin",
        "POST",
        f"{SCIM}/Users",
        {"schemas": [SCIM_USER], "userName": "erin"},
        id="scim-user-create",
    ),
    pytest.param("account", "bob", "DELETE", f"{SCIM}/Users/bob", None, id="scim-user"),
    pytest.param("group", "bob-team", "DELETE", f"{SCIM}/Groups/bob-team", None, id="scim-group"),
]


def _grant(
    *,
    client: TestClient,
    headers: dict[str, str],
    kind: str = "account",
    password: str,
    target: str,
) -> str:
    """The grant the caller behind *headers* earns for the *kind* named *target* with *password*."""
    body = {"kind": kind, "password": password, "target": target}
    resp = client.post(REAUTH, headers=headers, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["grant"]


def _hold_to_grants(*, app: FastAPI) -> None:
    """Lift the test app's re-auth waiver, so every access change needs its grant."""
    for check in REAUTH_CHECKS:
        app.dependency_overrides.pop(check, None)


@pytest.fixture
def short_token(admin_headers: dict[str, str], app: FastAPI, client: TestClient) -> str:
    """Store bob, carol, bob-team, bob-role, bob-idp and bob-key, then hold every access change to its grant."""
    for username in ("bob", "carol"):
        resp = client.post(ACCOUNTS, headers=admin_headers, json=_account_body(username))
        assert resp.status_code == 201, resp.text
    app.state.group_store.create("bob-team", [], members=["bob"])
    seeds = [
        (ROLES, {"name": "bob-role", "permissions": ["group:read"]}),
        (PROVIDERS, _provider_body("bob-idp")),
        (API_KEYS, {"name": "bob-key"}),
    ]
    for path, body in seeds:
        resp = client.post(path, headers=admin_headers, json=body)
        assert resp.status_code in (200, 201), resp.text
    token = resp.json()["short_token"]
    _hold_to_grants(app=app)
    return token


class TestAccessReauth:
    @pytest.mark.parametrize(("method", "path", "body"), WRITES)
    def test_a_change_without_a_grant_is_refused(
        self,
        admin_headers: dict[str, str],
        client: TestClient,
        short_token: str,
        method: str,
        path: str,
        body: dict | None,
    ):
        resp = client.request(
            method, path.format(short_token=short_token), headers=admin_headers, json=body
        )
        assert (resp.status_code, resp.json().get("code")) == (403, "reauth_required"), resp.text

    @pytest.mark.parametrize(("method", "path", "body", "expected_status"), REFUSED_FIRST)
    def test_a_write_that_would_be_refused_answers_before_the_grant(
        self,
        admin_headers: dict[str, str],
        client: TestClient,
        short_token: str,
        method: str,
        path: str,
        body: dict,
        expected_status: int,
    ):
        resp = client.request(method, path, headers=admin_headers, json=body)

        assert resp.status_code == expected_status, resp.text
        assert resp.json().get("code") != "reauth_required"

    @pytest.mark.parametrize(("kind", "target", "method", "path", "body"), OPENED)
    def test_a_grant_for_the_target_opens_it(
        self,
        admin_headers: dict[str, str],
        client: TestClient,
        short_token: str,
        kind: str,
        target: str,
        method: str,
        path: str,
        body: dict | None,
    ):
        grant = _grant(
            client=client,
            headers=admin_headers,
            kind=kind,
            password=ADMIN_PASSWORD,
            target=target.format(short_token=short_token),
        )
        resp = client.request(
            method,
            path.format(short_token=short_token),
            headers={**admin_headers, REAUTH_HEADER: grant},
            json=body,
        )
        assert resp.status_code < 300, resp.text

    @pytest.mark.parametrize(
        ("kind", "expected_status"), [("group", 200), ("account", 403)], ids=["group", "account"]
    )
    def test_only_a_grant_of_the_right_kind_opens_it(
        self,
        admin_headers: dict[str, str],
        client: TestClient,
        short_token: str,
        kind: str,
        expected_status: int,
    ):
        grant = _grant(
            client=client,
            headers=admin_headers,
            kind=kind,
            password=ADMIN_PASSWORD,
            target="bob-team",
        )
        resp = client.put(
            f"{GROUPS}/bob-team",
            headers={**admin_headers, REAUTH_HEADER: grant},
            json={"description": "Bob's"},
        )
        assert resp.status_code == expected_status, resp.text

    def test_a_grant_for_another_target_is_refused(
        self, admin_headers: dict[str, str], client: TestClient, short_token: str
    ):
        grant = _grant(
            client=client, headers=admin_headers, password=ADMIN_PASSWORD, target="carol"
        )
        resp = client.put(
            f"{ACCOUNTS}/bob",
            headers={**admin_headers, REAUTH_HEADER: grant},
            json={"name": "Bobby"},
        )
        assert (resp.status_code, resp.json().get("code")) == (403, "reauth_required"), resp.text

    def test_another_callers_grant_is_refused(
        self,
        admin_headers: dict[str, str],
        client: TestClient,
        operator_headers: dict[str, str],
        short_token: str,
    ):
        grant = _grant(
            client=client, headers=operator_headers, password="test-operator-pw", target="bob"
        )
        resp = client.put(
            f"{ACCOUNTS}/bob",
            headers={**admin_headers, REAUTH_HEADER: grant},
            json={"name": "Bobby"},
        )
        assert (resp.status_code, resp.json().get("code")) == (403, "reauth_required"), resp.text

    @pytest.mark.parametrize(
        ("audience", "lifetime"),
        [(REAUTH_AUDIENCE, timedelta(minutes=-10)), (LINK_TICKET_AUDIENCE, timedelta(minutes=5))],
        ids=["expired", "another_audience"],
    )
    def test_a_token_that_is_no_live_grant_is_refused(
        self,
        admin_headers: dict[str, str],
        api_settings: DFESettings,
        client: TestClient,
        short_token: str,
        audience: str,
        lifetime: timedelta,
    ):
        claims = {"aud": audience, "sub": "admin", "target": "bob"}
        token = jwt_authority_for(api_settings).sign(claims=claims, expires_delta=lifetime)
        resp = client.put(
            f"{ACCOUNTS}/bob",
            headers={**admin_headers, REAUTH_HEADER: token},
            json={"name": "Bobby"},
        )
        assert (resp.status_code, resp.json().get("code")) == (403, "reauth_required"), resp.text

    def test_a_wrong_password_earns_nothing(
        self, admin_headers: dict[str, str], client: TestClient, short_token: str
    ):
        resp = client.post(
            REAUTH, headers=admin_headers, json={"password": "not-the-password", "target": "bob"}
        )
        assert (resp.status_code, resp.json()["code"]) == (403, "invalid_password")

    def test_an_identity_providers_account_is_sent_to_its_provider(
        self, api_settings: DFESettings, app: FastAPI, client: TestClient, short_token: str
    ):
        accounts = app.state.account_store
        accounts.create("ida", "", groups=[])
        accounts.update("ida", external=True, source_provider="scim", subject="ida")
        token = session_token(app, api_settings, {"sub": "ida"})
        resp = client.post(
            REAUTH,
            headers={"Authorization": f"Bearer {token}"},
            json={"password": "anything", "target": "bob"},
        )
        assert (resp.status_code, resp.json()["code"]) == (409, "reauth_via_idp")

    def test_an_api_key_needs_no_grant(self, app: FastAPI, client: TestClient, short_token: str):
        _, key = app.state.api_key_store.create("ops-key", groups=["dfe-admins"])
        resp = client.put(f"{ACCOUNTS}/bob", headers={"X-API-Key": key}, json={"name": "Bobby"})
        assert resp.status_code == 200, resp.text


class TestRetireAdminReauth:
    @pytest.mark.parametrize(
        ("granted", "expected_reauth_required"),
        [(False, True), (True, False)],
        ids=["no_grant", "grant"],
    )
    def test_another_admin_needs_a_grant(
        self,
        admin_headers: dict[str, str],
        api_settings: DFESettings,
        app: FastAPI,
        client: TestClient,
        granted: bool,
        expected_reauth_required: bool,
    ):
        body = _account_body("ada", groups=["dfe-admins"])
        resp = client.post(ACCOUNTS, headers=admin_headers, json=body)
        assert resp.status_code == 201, resp.text
        _hold_to_grants(app=app)
        headers = {"Authorization": f"Bearer {session_token(app, api_settings, {'sub': 'ada'})}"}
        if granted:
            target = admin_account_name(api_settings.auth.local.admin_name)
            headers[REAUTH_HEADER] = _grant(
                client=client, headers=headers, password=PASSWORD, target=target
            )
        resp = client.post(RETIRE, headers=headers)
        assert (resp.json().get("code") == "reauth_required") is expected_reauth_required, resp.text

    def test_the_admin_retiring_itself_needs_none(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        _hold_to_grants(app=app)
        resp = client.post(RETIRE, headers=admin_headers)
        assert resp.json().get("code") != "reauth_required", resp.text
