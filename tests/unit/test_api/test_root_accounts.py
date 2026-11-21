#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_root_accounts.py
#  Purpose:      API tests for root accounts, which name no org and are bound to your org
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dfe_engine.settings import DFESettings
from tests.support.accounts import session_token
from tests.unit.test_api.root_accounts_cases import (
    ORG_DISPLAY_NAME_CASES,
    ORG_FILTER_CASES,
    SESSION_ORG_CASES,
    OrgDisplayNameCase,
    OrgFilterCase,
    SessionOrgCase,
)

_PASSWORD = "kim-local-password-1"


def _name_your_org(*, app: FastAPI, name: str | None) -> None:
    """Register acme, globex and initech (no display name), and name *name* as your org unless it is None."""
    for org, display_name in (("acme", "Acme Corp"), ("globex", "Globex Inc"), ("initech", "")):
        app.state.org_registry.create(display_name=display_name, name=org)
    if name is not None:
        app.state.org_registry.home.set(actor="test", name=name)


def _store_kim(*, app: FastAPI, org: str) -> None:
    """Store kim naming *org*, holding data_viewer through a system group."""
    app.state.account_store.create(username="kim", password=_PASSWORD, org=org)
    app.state.group_store.create(members=["kim"], name="kim-viewers", roles=["data_viewer"])


class TestSessionOrg:
    @pytest.mark.parametrize(
        "case", SESSION_ORG_CASES, ids=[case["id"] for case in SESSION_ORG_CASES]
    )
    def test_matches_expected(
        self, api_settings: DFESettings, app: FastAPI, client: TestClient, case: SessionOrgCase
    ):
        _name_your_org(app=app, name=case["your_org"])
        _store_kim(app=app, org=case["account_org"])
        token = session_token(app, api_settings, {"sub": "kim"})
        me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        login = client.post(
            "/api/v1/auth/login", json={"password": _PASSWORD, "username": "kim"}
        ).json()
        claims = jwt.decode(login["access_token"], options={"verify_signature": False})
        assert (me.json()["org_id"], claims["org_id"]) == (
            case["expected_org_id"],
            case["expected_org_id"],
        )


class TestListFilter:
    @pytest.mark.parametrize(
        "case", ORG_FILTER_CASES, ids=[case["id"] for case in ORG_FILTER_CASES]
    )
    def test_org(
        self,
        admin_headers: dict[str, str],
        app: FastAPI,
        client: TestClient,
        case: OrgFilterCase,
    ):
        _name_your_org(app=app, name="acme")
        for username, org in (("acme-amy", "acme"), ("globex-bob", "globex"), ("root-kim", "")):
            app.state.account_store.create(username=username, password="", org=org)
        page = client.get(
            "/api/v1/auth/accounts",
            headers=admin_headers,
            params={"limit": 100, "org": case["org"]},
        ).json()
        listed = {item["username"] for item in page["items"]}
        assert listed & {"acme-amy", "globex-bob", "root-kim"} == case["expected"]


class TestOrgDisplayName:
    @pytest.mark.parametrize(
        "case", ORG_DISPLAY_NAME_CASES, ids=[case["id"] for case in ORG_DISPLAY_NAME_CASES]
    )
    def test_matches_expected(
        self,
        admin_headers: dict[str, str],
        app: FastAPI,
        client: TestClient,
        case: OrgDisplayNameCase,
    ):
        _name_your_org(app=app, name=case["your_org"])
        _store_kim(app=app, org=case["account_org"])
        body = client.get("/api/v1/auth/accounts/kim", headers=admin_headers).json()
        assert body["org_display_name"] == case["expected_org_display_name"]
