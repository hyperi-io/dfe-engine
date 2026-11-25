#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_org_delete_cascade.py
#  Purpose:      Deleting an org deletes its accounts, API keys and sign-in providers, and nothing of another org's
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dfe_engine.auth.oidc.models import OIDCProvider
from tests.support.present import present

ORGS = "/api/v1/orgs"
PASSWORD = "Correct-Horse-Battery-9"


def _bound(*, app: FastAPI, org: str) -> dict[str, list[str]]:
    """The accounts, API keys and sign-in providers bound to *org*."""
    return {
        "accounts": sorted(
            account.username for account in app.state.account_store.list() if account.org == org
        ),
        "keys": sorted(key.name for key in app.state.api_key_store.list() if key.org == org),
        "providers": sorted(
            name
            for name, provider in app.state.oidc_provider_registry.list()
            if provider.org == org
        ),
    }


def _seed(*, admin_headers: dict[str, str], app: FastAPI, client: TestClient, org: str) -> None:
    """Create *org* with one account, one API key and one sign-in provider of its own."""
    resp = client.post(ORGS, headers=admin_headers, json={"name": org})
    assert resp.status_code == 201, resp.text
    app.state.account_store.create(f"{org}-user", PASSWORD, groups=[f"{org}-viewers"], org=org)
    app.state.api_key_store.create(f"{org}-key", groups=[f"{org}-viewers"], org=org)
    app.state.oidc_provider_registry.create(
        f"{org}-idp",
        OIDCProvider(issuer=f"https://{org}.example", org=org, type="generic"),
    )


@pytest.fixture
def two_orgs(admin_headers: dict[str, str], app: FastAPI, client: TestClient) -> None:
    """globex and initech, each with an account, an API key and a sign-in provider."""
    for org in ("globex", "initech"):
        _seed(admin_headers=admin_headers, app=app, client=client, org=org)


class TestOrgDelete:
    def test_takes_what_is_bound_to_it(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient, two_orgs: None
    ):
        resp = client.delete(f"{ORGS}/globex", headers=admin_headers)
        assert resp.status_code == 204, resp.text

        assert _bound(app=app, org="globex") == {"accounts": [], "keys": [], "providers": []}
        assert _bound(app=app, org="initech") == {
            "accounts": ["initech-user"],
            "keys": ["initech-key"],
            "providers": ["initech-idp"],
        }

    def test_a_refused_delete_takes_nothing(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient, two_orgs: None
    ):
        present(app.state.org_registry.home).set(actor="test", name="globex")
        before = _bound(app=app, org="globex")

        resp = client.delete(f"{ORGS}/globex", headers=admin_headers)

        assert (resp.status_code, resp.json()["code"]) == (409, "home_org")
        assert _bound(app=app, org="globex") == before
