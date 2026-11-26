#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_admin_write_reauth.py
#  Purpose:      Org, ingest credential and system setting writes that move access or lose data ask for a fresh sign-in
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dfe_engine.auth.reauth import REAUTH_HEADER
from tests.support.present import present
from tests.unit.test_api.conftest import ADMIN_PASSWORD
from tests.unit.test_api.test_account_reauth import _grant, _hold_to_grants

ORG = "/api/v1/orgs/bob-org"
CREDENTIAL = f"{ORG}/ingest-credentials/bob-cred"

# A write of each gated kind, as (kind, target, method, path, body).
GATED = [
    pytest.param("org", "bob-org", "DELETE", ORG, None, id="org-delete"),
    pytest.param("org", "bob-org", "PUT", ORG, {"org_ids": ["bob-org", "bob-eu"]}, id="org-ids"),
    pytest.param("org", "bob-org", "PUT", ORG, {"enabled": False}, id="org-disable"),
    pytest.param("credential", "bob-cred", "DELETE", CREDENTIAL, None, id="credential-delete"),
    pytest.param(
        "credential", "bob-cred", "POST", f"{CREDENTIAL}/rotate", None, id="credential-rotate"
    ),
    pytest.param(
        "setting",
        "organisation",
        "PUT",
        "/api/v1/system/organisation",
        {"name": "bob-org"},
        id="your-org",
    ),
    pytest.param(
        "setting",
        "retention",
        "PUT",
        "/api/v1/system/retention",
        {"default_ttl_days": 30},
        id="retention",
    ),
    pytest.param(
        "setting",
        "retention",
        "PATCH",
        "/api/v1/system/defaults",
        {"ttl_days": 7},
        id="defaults-retention",
    ),
]

# Writes that move no one's access, as (method, path, body).
UNGATED = [
    pytest.param("POST", "/api/v1/orgs", {"name": "new-org"}, id="org-create"),
    pytest.param("PUT", ORG, {"display_name": "Bob Org"}, id="org-rename"),
    pytest.param(
        "POST",
        "/api/v1/auth/roles",
        {"name": "new-role", "permissions": ["group:read"]},
        id="role-create",
    ),
    pytest.param(
        "PATCH",
        "/api/v1/system/defaults",
        {"common_header_type": None},
        id="defaults-without-retention",
    ),
]


@pytest.fixture
def bob_org(admin_headers: dict[str, str], app: FastAPI, client: TestClient) -> None:
    """Store bob-org, then hold every gated write to its grant."""
    for name in ("acme", "bob-org"):
        resp = client.post("/api/v1/orgs", headers=admin_headers, json={"name": name})
        assert resp.status_code == 201, resp.text
    # Your organisation is named, so naming another one is a change.
    present(app.state.org_registry.home).set(actor="test", name="acme")
    _hold_to_grants(app=app)


def _code(resp) -> str:
    """The problem ``code`` a response carries, else ``""``."""
    try:
        return resp.json().get("code", "")
    except ValueError:
        return ""


class TestAdminWriteReauth:
    @pytest.mark.parametrize(("kind", "target", "method", "path", "body"), GATED)
    def test_asks_then_lets_the_grant_through(
        self,
        admin_headers: dict[str, str],
        bob_org: None,
        client: TestClient,
        kind: str,
        target: str,
        method: str,
        path: str,
        body: dict | None,
    ):
        refused = client.request(method, path, headers=admin_headers, json=body)
        assert (refused.status_code, _code(refused)) == (403, "reauth_required"), refused.text

        grant = _grant(
            client=client, headers=admin_headers, kind=kind, password=ADMIN_PASSWORD, target=target
        )
        granted = client.request(
            method, path, headers={**admin_headers, REAUTH_HEADER: grant}, json=body
        )
        assert _code(granted) != "reauth_required", granted.text

    @pytest.mark.parametrize(("method", "path", "body"), UNGATED)
    def test_a_write_that_moves_no_access_does_not_ask(
        self,
        admin_headers: dict[str, str],
        bob_org: None,
        client: TestClient,
        method: str,
        path: str,
        body: dict,
    ):
        resp = client.request(method, path, headers=admin_headers, json=body)
        assert _code(resp) != "reauth_required", resp.text

    def test_an_id_another_org_holds_is_refused_before_the_grant_is_asked_for(
        self, admin_headers: dict[str, str], bob_org: None, client: TestClient
    ):
        resp = client.put(ORG, headers=admin_headers, json={"org_ids": ["bob-org", "acme"]})

        assert (resp.status_code, _code(resp)) == (409, "conflict"), resp.text

    def test_setup_names_the_first_organisation_without_asking(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        resp = client.post("/api/v1/orgs", headers=admin_headers, json={"name": "acme"})
        assert resp.status_code == 201, resp.text
        _hold_to_grants(app=app)

        named = client.put(
            "/api/v1/system/organisation", headers=admin_headers, json={"name": "acme"}
        )
        assert _code(named) != "reauth_required", named.text
