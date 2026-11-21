#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_org_role_rule.py
#  Purpose:      API tests for org roles: the rule that an org account holds only its own org's admin or viewer group, and what an org admin may do
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dfe_engine.auth.identity_links import LinkTicket
from dfe_engine.auth.jit import (
    JitEmailHeldError,
    JitIdentityCollisionError,
    JitLinkRequiredError,
    JitNoRoleError,
)
from dfe_engine.query.executor import ViewExecutor
from dfe_engine.settings import DFESettings
from tests.support.accounts import session_token
from tests.unit.test_api.org_role_rule_cases import (
    ACCOUNTS,
    ACME_SSO,
    LIVE_GRANTS_CASES,
    ORG_ADMIN_ACCOUNTS_CASES,
    ORG_ADMIN_PROVIDERS_CASES,
    ORG_ADMIN_ROLE_CASES,
    ORG_ROLE_CASES,
    PASSWORD,
    PROVIDERS,
    SCIM,
    LiveGrantsCase,
    OrgAdminAccountsCase,
    OrgAdminProvidersCase,
    OrgAdminRoleCase,
    OrgRoleCase,
)
from tests.unit.test_query.view_fakes import OneViewCatalog, RecordingClient, view_definition

EXECUTE = "/api/v1/queries/views/analytics/events/execute"
ME = "/api/v1/auth/me"


def _reach(*, client: TestClient, headers: dict[str, str]) -> tuple[list[str], list[str]]:
    """The roles and orgs the session behind *headers* holds, each sorted."""
    resp = client.get(ME, headers=headers)
    assert resp.status_code == 200, resp.text
    return sorted(resp.json()["roles"]), sorted(resp.json()["org_ids"])


@pytest.fixture
def acme_admin(api_settings: DFESettings, orgs: FastAPI) -> dict[str, str]:
    """Make kim acme's only admin beside ace, a local acme viewer, and gil, a globex viewer, and return kim's headers."""
    accounts = orgs.state.account_store
    groups = orgs.state.group_store
    for username, org in (("ace", "acme"), ("gil", "globex")):
        accounts.create(username, PASSWORD, groups=[f"{org}-viewers"])
        accounts.update(username, org=org, password_change_required=False)
        groups.add_member(f"{org}-viewers", username)
    groups.update("acme-viewers", members=["ace", "ida"])
    groups.add_member("acme-admins", "kim")
    token = session_token(orgs, api_settings, {"sub": "kim"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def providers(admin_headers: dict[str, str], client: TestClient, orgs: FastAPI) -> FastAPI:
    """Store acme-idp, globex-idp trusting globex.test, and the platform's platform-idp trusting example.test."""
    for name, org, domains in (
        ("acme-idp", "acme", []),
        ("globex-idp", "globex", ["globex.test"]),
        ("platform-idp", "", ["example.test"]),
    ):
        body = {
            "client_id": f"{name}-client",
            "issuer": f"https://{name}.test",
            "name": name,
            "org": org,
            "trusted_email_domains": domains,
            "type": "generic",
        }
        resp = client.post(PROVIDERS, headers=admin_headers, json=body)
        assert resp.status_code == 201, resp.text
    return orgs


@pytest.fixture
def orgs(admin_headers: dict[str, str], app: FastAPI, client: TestClient) -> FastAPI:
    """Name hyperi your org, create acme and globex with their groups, and store the accounts the cases read.

    kim is a local acme viewer. ida is an acme viewer its IdP owns, asserting ext-1 and ext-eng, the id the roleless system group eng links. ann belongs to hyperi and holds sys-analysts.
    """
    for name in ("hyperi", "acme", "globex"):
        resp = client.post("/api/v1/orgs", headers=admin_headers, json={"name": name})
        assert resp.status_code == 201, resp.text
    app.state.org_registry.home.set(actor="test", name="hyperi")
    groups = app.state.group_store
    groups.create("sys-analysts", ["data_analyst"])
    groups.create("acme-analysts", ["data_analyst"], scope="org:acme")
    groups.create("eng", [], source_id="ext-eng", source_provider="scim")
    kim = {
        "email": "kim@example.test",
        "groups": ["acme-viewers"],
        "name": "Kim",
        "org": "acme",
        "password": PASSWORD,
        "username": "kim",
    }
    ann = {
        "email": "ann@example.test",
        "groups": ["sys-analysts"],
        "name": "Ann",
        "org": "hyperi",
        "password": PASSWORD,
        "username": "ann",
    }
    for body in (kim, ann):
        resp = client.post(ACCOUNTS, headers=admin_headers, json=body)
        assert resp.status_code == 201, resp.text
    accounts = app.state.account_store
    accounts.create("ida", "", groups=[])
    accounts.update(
        "ida", external=True, groups=["ext-1", "ext-eng"], org="acme", source_provider="scim"
    )
    groups.add_member("acme-viewers", "ida")
    for username in ("ann", "kim"):
        accounts.update(username, password_change_required=False)
    return app


class TestOrgRoleRule:
    @pytest.mark.parametrize("case", ORG_ROLE_CASES, ids=[case["id"] for case in ORG_ROLE_CASES])
    def test_matches_expected(
        self,
        admin_headers: dict[str, str],
        client: TestClient,
        orgs: FastAPI,
        case: OrgRoleCase,
    ):
        resp = client.request(
            case["method"], case["path"], headers=admin_headers, json=case["body"]
        )
        code = resp.json().get("code") if resp.status_code == 422 else None
        expected_code = "org_role_only" if case["expected_status"] == 422 else None
        assert (resp.status_code, code) == (case["expected_status"], expected_code), resp.text

    def test_scim_adds_an_org_account_to_a_group_granting_nothing(
        self, admin_headers: dict[str, str], client: TestClient, orgs: FastAPI
    ):
        created = client.post(
            f"{SCIM}/Groups",
            headers=admin_headers,
            json={
                "displayName": "scim-grp",
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            },
        )
        assert created.status_code == 201, created.text
        added = client.patch(
            f"{SCIM}/Groups/scim-grp",
            headers=admin_headers,
            json={
                "Operations": [{"op": "add", "path": "members", "value": [{"value": "kim"}]}],
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            },
        )
        assert (added.status_code, orgs.state.group_store.get("scim-grp").members) == (200, ["kim"])


class TestLiveGrants:
    @pytest.mark.parametrize(
        "case", LIVE_GRANTS_CASES, ids=[case["id"] for case in LIVE_GRANTS_CASES]
    )
    def test_matches_expected(
        self,
        api_settings: DFESettings,
        client: TestClient,
        orgs: FastAPI,
        case: LiveGrantsCase,
    ):
        case["arrange"](app=orgs)
        token = session_token(orgs, api_settings, {"sub": case["username"]})
        reach = _reach(client=client, headers={"Authorization": f"Bearer {token}"})
        assert reach == (case["expected_roles"], case["expected_org_ids"])

    def test_org_key_reaches_only_its_org(self, client: TestClient, orgs: FastAPI):
        _, key = orgs.state.api_key_store.create(
            "acme-key", groups=["acme-viewers", "sys-analysts"], org="acme"
        )
        assert _reach(client=client, headers={"X-API-Key": key}) == (["org_viewer"], ["acme"])

    def test_password_login_claims_only_its_orgs_roles(self, client: TestClient, orgs: FastAPI):
        orgs.state.group_store.add_member("sys-analysts", "kim")
        resp = client.post("/api/v1/auth/login", json={"password": PASSWORD, "username": "kim"})
        assert (resp.status_code, resp.json().get("roles")) == (200, ["org_viewer"]), resp.text

    def test_idp_sign_in_granting_only_outside_its_org_is_refused(self, orgs: FastAPI):
        orgs.state.oidc_provider_registry.create("acme-sso", ACME_SSO)
        groups = orgs.state.group_store
        groups.update("acme-viewers", members=["ida"])
        groups.update("sys-analysts", source_id="sys-id", source_provider="acme-sso")
        ticket = LinkTicket(
            account="kim",
            email="kim@example.test",
            groups=["sys-id"],
            provider="acme-sso",
            subject="kim-sub",
        )
        message = (
            "OIDC login refused: no DFE group with a role is linked to your identity provider "
            "groups. Ask a DFE administrator to link one."
        )
        with pytest.raises(JitNoRoleError, match=re.escape(message)):
            orgs.state.jit_provisioner.complete_link(ticket=ticket)


class TestOrgAdminRole:
    @pytest.mark.parametrize(
        "case", ORG_ADMIN_ROLE_CASES, ids=[case["id"] for case in ORG_ADMIN_ROLE_CASES]
    )
    def test_matches_expected(
        self,
        admin_headers: dict[str, str],
        client: TestClient,
        orgs: FastAPI,
        case: OrgAdminRoleCase,
    ):
        resp = client.request(
            case["method"], case["path"], headers=admin_headers, json=case["body"]
        )
        code = resp.json().get("code") if resp.status_code >= 400 else None
        assert (resp.status_code, code) == (case["expected_status"], case["expected_code"]), (
            resp.text
        )

    def test_an_org_admin_reads_through_its_orgs_user(
        self, api_settings: DFESettings, client: TestClient, orgs: FastAPI
    ):
        orgs.state.group_store.add_member("acme-admins", "kim")
        token = session_token(orgs, api_settings, {"sub": "kim"})
        resp = client.get(ME, headers={"Authorization": f"Bearer {token}"})
        assert (resp.status_code, resp.json()["hyperdx_identity"]) == (200, "dfe_org_acme")


class TestOrgAdminAccounts:
    @pytest.mark.parametrize(
        "case", ORG_ADMIN_ACCOUNTS_CASES, ids=[case["id"] for case in ORG_ADMIN_ACCOUNTS_CASES]
    )
    def test_matches_expected(
        self, acme_admin: dict[str, str], client: TestClient, case: OrgAdminAccountsCase
    ):
        resp = client.request(case["method"], case["path"], headers=acme_admin, json=case["body"])
        code = resp.json().get("code") if resp.status_code >= 400 else None
        assert (resp.status_code, code) == (case["expected_status"], case["expected_code"]), (
            resp.text
        )

    def test_lists_only_its_orgs_accounts(self, acme_admin: dict[str, str], client: TestClient):
        resp = client.get(ACCOUNTS, headers=acme_admin)
        listed = sorted(item["username"] for item in resp.json()["items"])
        assert (resp.status_code, listed) == (200, ["ace", "ida", "kim"]), resp.text

    def test_a_second_admin_lets_it_step_down(self, acme_admin: dict[str, str], client: TestClient):
        promoted = client.put(
            f"{ACCOUNTS}/ace", headers=acme_admin, json={"groups": ["acme-admins"]}
        )
        demoted = client.put(
            f"{ACCOUNTS}/kim", headers=acme_admin, json={"groups": ["acme-viewers"]}
        )
        assert (promoted.status_code, demoted.status_code) == (200, 200), demoted.text

    def test_your_orgs_admin_may_remove_its_last_admin(
        self, acme_admin: dict[str, str], admin_headers: dict[str, str], client: TestClient
    ):
        resp = client.put(
            f"{ACCOUNTS}/kim", headers=admin_headers, json={"groups": ["acme-viewers"]}
        )
        assert resp.status_code == 200, resp.text


class TestOrgViews:
    def test_an_org_viewer_runs_its_orgs_isolated_view(
        self, api_settings: DFESettings, client: TestClient, orgs: FastAPI, reader: RecordingClient
    ):
        token = session_token(orgs, api_settings, {"sub": "kim"})
        resp = client.post(EXECUTE, headers={"Authorization": f"Bearer {token}"}, json={})
        assert (resp.status_code, reader.parameters.get("org_id")) == (200, "acme"), resp.text

    def test_an_org_viewer_may_not_run_a_view_of_every_org(
        self, api_settings: DFESettings, client: TestClient, orgs: FastAPI, reader: RecordingClient
    ):
        every_org = view_definition().model_copy(update={"tenant_isolated": False})
        orgs.state.view_executor = ViewExecutor(
            catalog=OneViewCatalog(every_org),  # ty: ignore[invalid-argument-type]  # a stand-in catalog serving one view
            database="dfe",
            restricted_client=reader,
        )
        token = session_token(orgs, api_settings, {"sub": "kim"})
        resp = client.post(EXECUTE, headers={"Authorization": f"Bearer {token}"}, json={})
        assert (resp.status_code, reader.sql) == (403, ""), resp.text


class TestOrgAdminProviders:
    @pytest.mark.parametrize(
        "case", ORG_ADMIN_PROVIDERS_CASES, ids=[case["id"] for case in ORG_ADMIN_PROVIDERS_CASES]
    )
    def test_matches_expected(
        self,
        acme_admin: dict[str, str],
        client: TestClient,
        providers: FastAPI,
        case: OrgAdminProvidersCase,
    ):
        resp = client.request(case["method"], case["path"], headers=acme_admin, json=case["body"])
        code = resp.json().get("code") if resp.status_code >= 400 else None
        assert (resp.status_code, code) == (case["expected_status"], case["expected_code"]), (
            resp.text
        )

    def test_lists_only_its_orgs_providers(
        self, acme_admin: dict[str, str], client: TestClient, providers: FastAPI
    ):
        resp = client.get(PROVIDERS, headers=acme_admin)
        listed = [item["name"] for item in resp.json()["items"]]
        assert (resp.status_code, listed) == (200, ["acme-idp"]), resp.text


class TestOrgProviderSignIn:
    def test_a_first_sign_in_lands_in_its_org_as_viewer(self, providers: FastAPI):
        account = providers.state.jit_provisioner.ensure_account("dana-sub", [], "acme-idp")
        viewers = providers.state.group_store.get("acme-viewers").members
        assert (account.org, account.username in viewers) == ("acme", True)

    def test_a_platform_provider_sign_in_holding_no_role_is_refused(self, providers: FastAPI):
        message = (
            "OIDC login refused: no DFE group with a role is linked to your identity provider "
            "groups. Ask a DFE administrator to link one."
        )
        with pytest.raises(JitNoRoleError, match=re.escape(message)):
            providers.state.jit_provisioner.ensure_account("eve-sub", [], "platform-idp")

    def test_an_org_provider_does_not_link_another_orgs_account(self, providers: FastAPI):
        providers.state.account_store.update("ann", email="ann@example.test", email_verified=True)
        message = (
            "OIDC login refused: another DFE account already uses this email. Ask a DFE "
            "administrator to trust your email domain on this identity provider."
        )
        with pytest.raises(JitEmailHeldError, match=re.escape(message)):
            providers.state.jit_provisioner.ensure_account(
                "ann-sub", [], "acme-idp", email="ann@example.test", email_verified=True
            )

    def test_a_platform_provider_links_the_account_holding_its_email(self, providers: FastAPI):
        providers.state.account_store.update("ann", email="ann@example.test", email_verified=True)
        message = "Sign in with this account's password to link your identity provider to it"
        with pytest.raises(JitLinkRequiredError, match=re.escape(message)):
            providers.state.jit_provisioner.ensure_account(
                "ann-sub", [], "platform-idp", email="ann@example.test", email_verified=True
            )


class TestOrgProviderFence:
    def test_its_provider_never_adopts_an_account_outside_its_org(self, providers: FastAPI):
        providers.state.account_store.update(
            "ann", external=True, source_provider="acme-idp", subject="ann"
        )
        message = "OIDC login refused"
        with pytest.raises(JitIdentityCollisionError, match=message) as refused:
            providers.state.jit_provisioner.ensure_account("ann", [], "acme-idp")
        assert refused.value.reason == "other_org"

    def test_a_platform_provider_does_not_link_an_org_account(self, providers: FastAPI):
        message = (
            "OIDC login refused: another DFE account already uses this email. Ask a DFE "
            "administrator to trust your email domain on this identity provider."
        )
        with pytest.raises(JitEmailHeldError, match=re.escape(message)):
            providers.state.jit_provisioner.ensure_account(
                "kim-sub", [], "platform-idp", email="kim@example.test", email_verified=True
            )

    def test_a_link_record_keeps_the_persons_org(self, providers: FastAPI):
        ticket = LinkTicket(
            account="kim",
            email="kim@example.test",
            groups=[],
            provider="acme-idp",
            subject="kim-sub",
        )
        providers.state.jit_provisioner.complete_link(ticket=ticket)
        assert providers.state.account_store.get("kim-sub").org == "acme"

    def test_deleting_its_provider_spares_accounts_outside_its_org(
        self, acme_admin: dict[str, str], client: TestClient, providers: FastAPI
    ):
        accounts = providers.state.account_store
        accounts.update("ann", oidc_id="acme-idp")
        providers.state.jit_provisioner.ensure_account("dana-sub", [], "acme-idp")
        resp = client.delete(f"{PROVIDERS}/acme-idp", headers=acme_admin)
        kept = (resp.status_code, accounts.get("ann").enabled, accounts.get("dana-sub"))
        assert kept == (200, True, None), resp.text

    def test_deleting_the_provider_every_admin_signs_in_through_is_refused(
        self, acme_admin: dict[str, str], client: TestClient, providers: FastAPI
    ):
        providers.state.account_store.update("kim", oidc_id="acme-idp")
        resp = client.delete(f"{PROVIDERS}/acme-idp", headers=acme_admin)
        assert (resp.status_code, resp.json()["code"]) == (409, "last_org_admin"), resp.text

    def test_naming_its_provider_after_a_stamp_outside_its_org_is_refused(
        self, acme_admin: dict[str, str], client: TestClient, providers: FastAPI
    ):
        providers.state.account_store.update("ann", oidc_id="legacy-idp")
        body = {
            "client_id": "legacy-client",
            "issuer": "https://legacy.test",
            "name": "legacy-idp",
            "org": "acme",
            "type": "generic",
        }
        resp = client.post(PROVIDERS, headers=acme_admin, json=body)
        assert (resp.status_code, resp.json()["code"]) == (422, "provider_name_reserved")

    def test_a_domain_clash_names_no_other_org(
        self, acme_admin: dict[str, str], client: TestClient, providers: FastAPI
    ):
        body = {
            "client_id": "acme-two-client",
            "issuer": "https://idp.acme.test",
            "name": "acme-two",
            "org": "acme",
            "trusted_email_domains": ["globex.test"],
            "type": "generic",
        }
        resp = client.post(PROVIDERS, headers=acme_admin, json=body)
        assert (resp.status_code, "globex-idp" in resp.json()["detail"]) == (422, False), resp.text

    def test_repointing_a_provider_holding_an_env_secret_is_refused(
        self,
        acme_admin: dict[str, str],
        admin_headers: dict[str, str],
        client: TestClient,
        providers: FastAPI,
    ):
        held = client.put(
            f"{PROVIDERS}/acme-idp", headers=admin_headers, json={"client_secret_env": "PATH"}
        )
        repointed = client.put(
            f"{PROVIDERS}/acme-idp", headers=acme_admin, json={"issuer": "https://elsewhere.test"}
        )
        codes = (held.status_code, repointed.status_code, repointed.json().get("code"))
        assert codes == (200, 422, "env_source_held"), repointed.text

    @pytest.mark.parametrize(
        ("name", "org", "expected_status"),
        [("acme-idp", "", 422), ("platform-idp", "acme", 200)],
        ids=["away_from_its_users_is_refused", "with_no_users_passes"],
    )
    def test_moving_a_provider(
        self,
        admin_headers: dict[str, str],
        client: TestClient,
        providers: FastAPI,
        name: str,
        org: str,
        expected_status: int,
    ):
        providers.state.jit_provisioner.ensure_account("dana-sub", [], "acme-idp")
        resp = client.put(f"{PROVIDERS}/{name}", headers=admin_headers, json={"org": org})
        assert resp.status_code == expected_status, resp.text
