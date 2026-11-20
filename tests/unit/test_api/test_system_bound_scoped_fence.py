#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_system_bound_scoped_fence.py
#  Purpose:      A scoped role bound at system scope reaches only the orgs its group names
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A ``scoped`` role bound system-wide over ``acme`` answers no at ``beta``.

The caller holds a scoped role with the org, group, repository, API-key and account
permissions, through a system group naming ``acme``. Each route below decided an
org-scope check, or a system-scope read nobody held to an org, and let it into
``beta`` before the fence. Every refusal has a control: the same caller at ``acme``,
or an admin, still passes.
"""

from collections.abc import Iterator

import pytest
from common.fake_repository_ch import FakeRepositoryCH
from fastapi.testclient import TestClient

from dfe_engine.api.deps import get_clickhouse_client
from dfe_engine.repository.store import RepositoryStore
from tests.support.held_callers import caller_in_group, define_role

ORGS = "/api/v1/orgs"
GROUPS = "/api/v1/auth/groups"
OBJECTS = "/api/v1/repository/objects"
CREATE = {"If-None-Match": "*"}

TENANT_PERMISSIONS = [
    "org:read",
    "org:write",
    "org:ingest_credentials:write",
    "group:read",
    "group:write",
    "group:add_member",
    "group:remove_member",
    "repository:read",
    "repository:write",
    "api_key:read",
    "account:read",
]

UNHELD_PERMISSIONS = [
    "account:read",
    "account:attributes:read",
    "account:attributes:read_sensitive",
    "group:attributes:read",
    "group:attributes:read_sensitive",
    "alert:read",
    "hunt:read",
]


@pytest.fixture(autouse=True)
def _reset_schema_flag():
    RepositoryStore._ensured_databases.clear()
    yield
    RepositoryStore._ensured_databases.clear()


@pytest.fixture
def fake_ch(app) -> Iterator[FakeRepositoryCH]:
    ch = FakeRepositoryCH(applied={("dfe", "repository")})
    app.dependency_overrides[get_clickhouse_client] = lambda: ch
    yield ch
    app.dependency_overrides.pop(get_clickhouse_client, None)


@pytest.fixture
def orgs(client: TestClient, app, admin_headers, fake_ch) -> None:
    """``acme`` with tenant ids of its own, ``beta`` with its name, and a group in each."""
    define_role(app, "tenant", TENANT_PERMISSIONS, scoped=True)
    for body in ({"name": "acme", "org_ids": ["acme-prod"]}, {"name": "beta"}):
        resp = client.post(ORGS, json=body, headers=admin_headers)
        assert resp.status_code == 201, resp.text
    for org in ("acme", "beta"):
        resp = client.post(
            GROUPS,
            json={"name": f"{org}-team", "roles": ["tenant"], "scope": f"org:{org}"},
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text


def _tenant(app, api_settings, name: str, marker: str = "acme") -> TestClient:
    """A caller whose scoped role is bound at system scope over the org ``marker`` names."""
    return caller_in_group(app, api_settings, name, roles=["tenant"], org_ids=[marker])


@pytest.fixture
def tenant(app, api_settings, orgs) -> TestClient:
    return _tenant(app, api_settings, "tenant-by-name")


def _refused(resp) -> None:
    assert resp.status_code == 403, resp.text
    assert "denied" in resp.json()["detail"], resp.text


class TestOrgRoutes:
    def test_get_refuses_the_org_the_group_does_not_name(self, tenant):
        _refused(tenant.get(f"{ORGS}/beta"))

    def test_get_still_reads_the_org_the_group_names(self, tenant):
        resp = tenant.get(f"{ORGS}/acme")

        assert resp.status_code == 200, resp.text

    def test_list_holds_the_caller_to_its_orgs(self, tenant, client, admin_headers):
        seen = {org["name"] for org in tenant.get(ORGS).json()["items"]}
        everything = {
            org["name"] for org in client.get(ORGS, headers=admin_headers).json()["items"]
        }

        assert seen == {"acme"}
        assert {"acme", "beta"} <= everything

    def test_a_tenant_id_marker_names_its_org(self, app, api_settings, orgs):
        by_tenant_id = _tenant(app, api_settings, "tenant-by-id", marker="acme-prod")

        assert by_tenant_id.get(f"{ORGS}/acme").status_code == 200
        _refused(by_tenant_id.get(f"{ORGS}/beta"))
        assert {org["name"] for org in by_tenant_id.get(ORGS).json()["items"]} == {"acme"}


class TestIngestCredentials:
    def test_a_credential_cannot_bind_an_org_the_group_does_not_name(self, tenant):
        resp = tenant.put(
            f"{ORGS}/acme/ingest-credentials/agents",
            json={"orgs": ["acme", "beta"]},
            headers=CREATE,
        )

        _refused(resp)

    def test_a_credential_cannot_be_written_under_another_org(self, tenant):
        resp = tenant.put(f"{ORGS}/beta/ingest-credentials/agents", json={}, headers=CREATE)

        _refused(resp)

    def test_a_credential_in_the_named_org_is_still_written(self, tenant):
        resp = tenant.put(f"{ORGS}/acme/ingest-credentials/agents", json={}, headers=CREATE)

        assert resp.status_code == 201, resp.text


class TestGroups:
    def test_a_group_cannot_be_created_in_another_org(self, tenant):
        resp = tenant.post(
            GROUPS, json={"name": "sneaky", "roles": ["tenant"], "scope": "org:beta"}
        )

        _refused(resp)

    def test_a_group_in_the_named_org_is_still_created(self, tenant):
        resp = tenant.post(
            GROUPS, json={"name": "acme-extra", "roles": ["tenant"], "scope": "org:acme"}
        )

        assert resp.status_code == 201, resp.text

    def test_members_of_another_orgs_group_cannot_be_added_or_removed(self, tenant):
        added = tenant.post(f"{GROUPS}/beta-team/members", json={"username": "someone"})
        removed = tenant.delete(f"{GROUPS}/beta-team/members/someone")

        assert {added.status_code, removed.status_code} == {404}

    def test_members_of_the_named_orgs_group_can_be_added(self, tenant, app):
        app.state.account_store.create("someone", "someone-password-2026")

        resp = tenant.post(f"{GROUPS}/acme-team/members", json={"username": "someone"})

        assert resp.status_code == 200, resp.text


class TestRepository:
    def test_an_org_layer_object_cannot_be_written_in_another_org(self, tenant):
        resp = tenant.put(f"{OBJECTS}/org/beta/notes/key", content=b"x")

        _refused(resp)

    def test_an_org_layer_object_in_the_named_org_is_still_written(self, tenant):
        resp = tenant.put(f"{OBJECTS}/org/acme/notes/key", content=b"x")

        assert resp.status_code == 200, resp.text

    def test_another_orgs_object_is_not_read(self, tenant, client, admin_headers):
        assert (
            client.put(
                f"{OBJECTS}/org/beta/notes/key", content=b"x", headers=admin_headers
            ).status_code
            == 200
        )

        assert tenant.get(f"{OBJECTS}/org/beta/notes/key").status_code == 404

    def test_another_users_layer_is_not_read(self, tenant, client, admin_headers):
        assert (
            client.put(
                f"{OBJECTS}/user/someone-else/notes/key", content=b"x", headers=admin_headers
            ).status_code
            == 200
        )

        assert tenant.get(f"{OBJECTS}/user/someone-else/notes/key").status_code == 404


class TestApiKeysAndScim:
    @pytest.mark.parametrize(
        "path",
        [
            pytest.param("/api/v1/auth/api-keys", id="api-keys"),
            pytest.param("/api/v1/scim/v2/Users", id="scim-users"),
            pytest.param("/api/v1/scim/v2/Groups", id="scim-groups"),
        ],
    )
    def test_a_system_read_nobody_holds_to_an_org_is_refused(self, tenant, path):
        _refused(tenant.get(path))

    @pytest.mark.parametrize(
        "path",
        [
            pytest.param("/api/v1/auth/api-keys", id="api-keys"),
            pytest.param("/api/v1/scim/v2/Users", id="scim-users"),
            pytest.param("/api/v1/scim/v2/Groups", id="scim-groups"),
        ],
    )
    def test_the_same_read_still_passes_for_an_admin(self, client, admin_headers, path):
        assert client.get(path, headers=admin_headers).status_code == 200


class TestBuiltInOrgViewer:
    """The built-in fenced role keeps the system-scope reads its handlers hold to its orgs."""

    @pytest.fixture
    def viewer(self, app, api_settings, orgs) -> TestClient:
        return caller_in_group(
            app, api_settings, "viewer-acme", roles=["org_viewer"], org_ids=["acme"]
        )

    @pytest.mark.parametrize(
        "path",
        [
            pytest.param("/api/v1/sources", id="sources"),
            pytest.param("/api/v1/samples", id="samples"),
        ],
    )
    def test_a_held_read_still_answers(self, viewer, path):
        assert viewer.get(path).status_code == 200

    @pytest.mark.parametrize(
        "path",
        [
            pytest.param("/api/v1/auth/accounts", id="accounts"),
            pytest.param("/api/v1/hunts", id="hunts"),
            pytest.param("/api/v1/orgs/beta", id="another-org"),
        ],
    )
    def test_a_read_it_does_not_hold_is_refused(self, viewer, path):
        _refused(viewer.get(path))


# Each read gated on a system-scope role check that no handler holds to the caller's orgs.
UNHELD_READS = [
    pytest.param("/api/v1/auth/accounts", id="accounts-list"),
    pytest.param("/api/v1/auth/accounts/admin", id="account"),
    pytest.param("/api/v1/auth/accounts/admin/git-status", id="account-git-status"),
    pytest.param("/api/v1/auth/accounts/admin/attributes", id="account-attributes"),
    pytest.param("/api/v1/auth/accounts/admin/sensitive-attributes", id="account-sensitive"),
    pytest.param("/api/v1/auth/groups/acme-team/attributes", id="group-attributes"),
    pytest.param("/api/v1/auth/groups/acme-team/sensitive-attributes", id="group-sensitive"),
    pytest.param("/api/v1/alerts/destinations", id="alert-destinations"),
    pytest.param("/api/v1/alerts/destinations/oncall", id="alert-destination"),
    pytest.param("/api/v1/hunts", id="hunts"),
    pytest.param("/api/v1/hunts/windows_hunt", id="hunt"),
]


class TestUnheldSystemReads:
    @pytest.fixture
    def reader(self, app, api_settings, orgs) -> TestClient:
        define_role(app, "tenant_reader", UNHELD_PERMISSIONS, scoped=True)
        return caller_in_group(
            app, api_settings, "reader-acme", roles=["tenant_reader"], org_ids=["acme"]
        )

    @pytest.mark.parametrize("path", UNHELD_READS)
    def test_a_system_bound_scoped_role_is_refused(self, reader, path):
        _refused(reader.get(path))

    @pytest.mark.parametrize("path", UNHELD_READS)
    def test_an_admin_is_not_refused(self, client, admin_headers, path):
        assert client.get(path, headers=admin_headers).status_code != 403


class TestSystemGroupWrite:
    def test_a_system_scope_group_it_creates_reaches_no_org(
        self, tenant, app, client, admin_headers
    ):
        """The scope check passes at system scope, and the group names no org to join."""
        created = tenant.post(
            GROUPS, json={"name": "sys-grp", "roles": ["tenant"], "scope": "system"}
        )

        assert created.status_code == 201, created.text
        group = app.state.group_store.get("sys-grp")
        assert group is not None
        assert group.org_markers == ()
        joined = tenant.post(f"{GROUPS}/sys-grp/members", json={"username": "tenant-by-name"})
        assert joined.status_code == 200, joined.text
        assert {org["name"] for org in tenant.get(ORGS).json()["items"]} == {"acme"}
        assert tenant.get(f"{ORGS}/beta").status_code == 403


class TestGroupNameOracle:
    body = {"name": "beta-team", "roles": ["tenant"], "scope": "org:acme"}

    def test_a_fenced_writer_is_not_told_another_orgs_group_exists(self, tenant):
        resp = tenant.post(GROUPS, json=self.body)

        assert resp.status_code == 409, resp.text
        assert "not available" in resp.json()["detail"]

    def test_an_admin_is_told(self, client, admin_headers, orgs):
        resp = client.post(GROUPS, json=self.body, headers=admin_headers)

        assert resp.status_code == 409, resp.text
        assert "already exists" in resp.json()["detail"]
