#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_group_scope_probe.py
#  Purpose:      An org-scoped group writer learns and takes nothing outside its org
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""An org-scoped group:write holder cannot probe or squat group names and provider ids.

orgadmin holds ``admin`` at org ``acme`` only. Every answer it gets about a group or
an identity-provider id outside acme must read the same whether that group or id
exists or not, and it must not be able to take an id a system group would need.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dfe_engine.api.deps import create_access_token
from dfe_engine.settings import DFESettings

GROUPS = "/api/v1/auth/groups"
ORGS = "/api/v1/orgs"
SYSTEM_SOURCE_ID = "domain-admins-sso"


@pytest.fixture
def orgadmin(
    app: FastAPI, client: TestClient, admin_headers: dict[str, str], api_settings: DFESettings
) -> dict[str, str]:
    """Headers for orgadmin, admin of acme alone, with a system group and a globex group linked."""
    for org in ("acme", "globex"):
        resp = client.post(ORGS, json={"name": org}, headers=admin_headers)
        assert resp.status_code == 201, resp.text
    app.state.account_store.create("orgadmin", "pw-orgadmin-123456")
    for body in (
        {"name": "acme-admins", "roles": ["admin"], "scope": "org:acme", "members": ["orgadmin"]},
        {"name": "acme-eng", "roles": ["data_viewer"], "scope": "org:acme"},
        {"name": "globex-analysts", "roles": ["data_analyst"], "scope": "org:globex"},
        {
            "name": "secops",
            "roles": ["admin"],
            "source_id": SYSTEM_SOURCE_ID,
            "source_provider": "oidc",
        },
    ):
        resp = client.post(GROUPS, json=body, headers=admin_headers)
        assert resp.status_code == 201, resp.text
    token = create_access_token(data={"sub": "orgadmin"}, settings=api_settings)
    return {"Authorization": f"Bearer {token}"}


def _answer(resp) -> tuple[int, str, str]:
    body = resp.json()
    return resp.status_code, body.get("code", ""), body.get("message", "")


class TestALinkOutsideTheOrgCannotBeProbed:
    def test_a_taken_id_and_a_free_one_answer_alike(
        self, client: TestClient, orgadmin: dict[str, str]
    ):
        taken = client.put(
            f"{GROUPS}/acme-eng",
            json={"source_id": SYSTEM_SOURCE_ID, "source_provider": "oidc"},
            headers=orgadmin,
        )
        free = client.put(
            f"{GROUPS}/acme-eng",
            json={"source_id": "never-linked-anywhere", "source_provider": "oidc"},
            headers=orgadmin,
        )

        assert _answer(taken) == _answer(free)
        assert taken.status_code == 403, taken.text

    def test_a_new_group_with_a_taken_id_and_with_a_free_one_answer_alike(
        self, client: TestClient, orgadmin: dict[str, str]
    ):
        def create(name: str, source_id: str):
            body = {
                "name": name,
                "roles": [],
                "scope": "org:acme",
                "source_id": source_id,
                "source_provider": "oidc",
            }
            return client.post(GROUPS, json=body, headers=orgadmin)

        taken = create("acme-probe-a", SYSTEM_SOURCE_ID)
        free = create("acme-probe-b", "never-linked-anywhere")

        assert (taken.status_code, taken.json()["code"]) == (free.status_code, free.json()["code"])
        assert taken.status_code == 403, taken.text


class TestAnOrgWriterCannotSquatAnId:
    def test_linking_its_own_group_is_refused_and_stores_nothing(
        self, app: FastAPI, client: TestClient, orgadmin: dict[str, str]
    ):
        resp = client.put(
            f"{GROUPS}/acme-eng",
            json={"source_id": "future-admins", "source_provider": "oidc"},
            headers=orgadmin,
        )

        assert resp.status_code == 403, resp.text
        assert app.state.group_store.get("acme-eng").source_id == ""

    def test_the_id_stays_free_for_a_system_group(
        self,
        app: FastAPI,
        client: TestClient,
        admin_headers: dict[str, str],
        orgadmin: dict[str, str],
    ):
        client.put(
            f"{GROUPS}/acme-eng",
            json={"source_id": "future-admins", "source_provider": "oidc"},
            headers=orgadmin,
        )

        resp = client.post(
            GROUPS,
            json={
                "name": "future-admins-group",
                "roles": ["admin"],
                "source_id": "future-admins",
                "source_provider": "oidc",
            },
            headers=admin_headers,
        )

        assert resp.status_code == 201, resp.text

    def test_a_system_admin_reclaims_an_id_an_org_group_already_holds(
        self,
        app: FastAPI,
        client: TestClient,
        admin_headers: dict[str, str],
        orgadmin: dict[str, str],
    ):
        app.state.group_store.update("acme-eng", source_id="squatted-id", source_provider="oidc")

        cleared = client.put(
            f"{GROUPS}/acme-eng",
            json={"source_id": "", "source_provider": ""},
            headers=admin_headers,
        )
        linked = client.put(
            f"{GROUPS}/secops",
            json={"source_id": "squatted-id", "source_provider": "oidc"},
            headers=admin_headers,
        )

        assert cleared.status_code == 200, cleared.text
        assert linked.status_code == 200, linked.text
        assert app.state.group_store.get("secops").source_id == "squatted-id"


class TestAGroupNameOutsideTheOrgCannotBeProbed:
    def test_a_name_another_org_holds_and_one_a_system_link_holds_answer_alike(
        self, client: TestClient, orgadmin: dict[str, str]
    ):
        def create(name: str):
            return client.post(
                GROUPS, json={"name": name, "roles": [], "scope": "org:acme"}, headers=orgadmin
            )

        group_name = create("globex-analysts")
        link_id = create(SYSTEM_SOURCE_ID)

        assert group_name.status_code == link_id.status_code == 409
        assert group_name.json()["code"] == link_id.json()["code"] == "conflict"
        assert "source_id" not in link_id.text
        assert group_name.json()["message"] == "Group name 'globex-analysts' is not available"
        assert link_id.json()["message"] == f"Group name '{SYSTEM_SOURCE_ID}' is not available"


class TestAGroupOutsideTheOrgReadsAsMissing:
    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("PUT", "{name}", {"description": "probe"}),
            ("POST", "{name}/members", {"username": "orgadmin"}),
            ("DELETE", "{name}/members/orgadmin", None),
            ("DELETE", "{name}", None),
        ],
        ids=["update", "add-member", "remove-member", "delete"],
    )
    @pytest.mark.parametrize("existing", ["globex-analysts", "secops"])
    def test_an_existing_group_answers_as_a_missing_one(
        self,
        client: TestClient,
        orgadmin: dict[str, str],
        method: str,
        path: str,
        body: dict | None,
        existing: str,
    ):
        def call(name: str):
            url = f"{GROUPS}/{path.format(name=name)}"
            return client.request(method, url, json=body, headers=orgadmin)

        present = call(existing)
        missing = call("no-such-group")

        assert present.status_code == missing.status_code == 404, present.text
        assert present.json()["code"] == missing.json()["code"] == "not_found"
        assert present.json()["message"] == f"Group '{existing}' not found"

    def test_its_own_group_still_answers_a_refusal_it_can_act_on(
        self, client: TestClient, orgadmin: dict[str, str]
    ):
        """A group the caller sees is not hidden: a refusal there names the reason."""
        resp = client.put(
            f"{GROUPS}/acme-eng",
            json={"source_id": "never-linked-anywhere", "source_provider": "oidc"},
            headers=orgadmin,
        )

        assert resp.status_code == 403, resp.text
        assert "system" in resp.json()["message"]
