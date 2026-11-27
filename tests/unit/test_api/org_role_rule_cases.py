#  Project:      dfe-engine
#  File:         tests/unit/test_api/org_role_rule_cases.py
#  Purpose:      Case tables for org roles: the rule that an org account holds only its own org's admin or viewer group, and what an org admin may do
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for org roles: the rule that an org account holds only its own org's admin or viewer group, and what an org admin may do."""

from typing import Any, Protocol, TypedDict

from fastapi import FastAPI

from dfe_engine.auth.identity_links import LinkTicket
from dfe_engine.auth.oidc.models import OIDCProvider

ACCOUNTS = "/api/v1/auth/accounts"

ACME_SSO = OIDCProvider(
    client_id="acme-sso-client", issuer="https://acme-sso.test", org="acme", type="generic"
)
"""acme's own provider, stored as acme-sso: its sign-ins reach acme's accounts only."""
API_KEYS = "/api/v1/auth/api-keys"
GROUPS = "/api/v1/auth/groups"
PASSWORD = "org-role-password-1"
PROVIDERS = "/api/v1/auth/oidc-providers"
SCIM = "/api/v1/scim/v2"


class Arrange(Protocol):
    """A write made straight to the stores, as a path the API's checks never see would make it."""

    def __call__(self, *, app: FastAPI) -> None: ...


class LiveGrantsCase(TypedDict):
    """An account's roles and orgs, read from its own session, after *arrange* runs on the fixture's stores."""

    id: str
    arrange: Arrange
    expected_org_ids: list[str]
    expected_roles: list[str]
    username: str


class OrgAdminAccountsCase(TypedDict):
    """A request kim makes as acme's only admin, with ace an acme viewer, gil a globex viewer and ann an account of your org."""

    id: str
    body: dict[str, Any] | None
    expected_code: str | None
    expected_status: int
    method: str
    path: str


class OrgAdminProvidersCase(TypedDict):
    """A request kim makes as acme's admin, with acme-idp, globex-idp (trusting globex.test) and the platform's platform-idp stored."""

    id: str
    body: dict[str, Any] | None
    expected_code: str | None
    expected_status: int
    method: str
    path: str


class OrgAdminRoleCase(TypedDict):
    """A group write naming org_admin, with the fixture's accounts and groups."""

    id: str
    body: dict[str, Any]
    expected_code: str | None
    expected_status: int
    method: str
    path: str


class OrgRoleCase(TypedDict):
    """A write while hyperi is your org, with the accounts and groups the fixture stores."""

    id: str
    body: dict[str, Any] | None
    expected_status: int
    method: str
    path: str


def _add_ann_to_a_system_org_admin_group(*, app: FastAPI) -> None:
    """Make ann a member of a system group granting org_admin."""
    app.state.group_store.create("sys-org-admins", ["org_admin"], members=["ann"])


def _add_kim_to_a_roleless_group_of_globex(*, app: FastAPI) -> None:
    """Make kim a member of a group with no roles tied to globex."""
    app.state.group_store.create("globex-misc", [], members=["kim"], scope="org:globex")


def _add_kim_to_a_system_group(*, app: FastAPI) -> None:
    """Make kim a member of sys-analysts."""
    app.state.group_store.add_member("sys-analysts", "kim")


def _add_kim_to_a_system_org_admin_group_of_acme(*, app: FastAPI) -> None:
    """Make kim a member of a system group granting org_admin over acme alone."""
    groups = app.state.group_store
    groups.create("acme-sys-admins", ["org_admin"], members=["kim"])
    groups.update("acme-sys-admins", org_ids=["acme"])


def _assert_a_system_group_for_ida(*, app: FastAPI) -> None:
    """Link sys-analysts to sys-id and have ida's IdP assert it, as a sign-in records."""
    app.state.group_store.update("sys-analysts", source_id="sys-id", source_provider="scim")
    app.state.account_store.update("ida", groups=["ext-1", "ext-eng", "sys-id"])


def _give_eng_a_role(*, app: FastAPI) -> None:
    """Give eng, which ida signs in through, the data_analyst role."""
    app.state.group_store.update("eng", roles=["data_analyst"])


def _leave_as_stored(*, app: FastAPI) -> None:
    """Write nothing."""


def _link_a_sign_in_to_kim(*, app: FastAPI) -> None:
    """Link sys-analysts to sys-id and link to kim a sign-in through acme-sso asserting it."""
    app.state.oidc_provider_registry.create("acme-sso", ACME_SSO)
    app.state.group_store.update("sys-analysts", source_id="sys-id", source_provider="acme-sso")
    ticket = LinkTicket(
        account="kim",
        email="kim@example.test",
        groups=["sys-id"],
        provider="acme-sso",
        subject="kim-sub",
    )
    app.state.jit_provisioner.complete_link(ticket=ticket)


def _link_a_system_group_to_ida(*, app: FastAPI) -> None:
    """Link sys-analysts to ext-1, which ida's IdP asserts."""
    app.state.group_store.update("sys-analysts", source_id="ext-1", source_provider="scim")


def _make_kim_an_acme_admin(*, app: FastAPI) -> None:
    """Swap kim's acme-viewers for acme-admins."""
    groups = app.state.group_store
    groups.update("acme-viewers", members=["ida"])
    groups.add_member("acme-admins", "kim")


def _move_kim_to_a_system_viewer_of_acme(*, app: FastAPI) -> None:
    """Swap kim's acme-viewers for a system group granting org_viewer over acme alone."""
    groups = app.state.group_store
    groups.update("acme-viewers", members=["ida"])
    groups.create("acme-readers", ["org_viewer"], members=["kim"])
    groups.update("acme-readers", org_ids=["acme"])


def _name_globex_your_org(*, app: FastAPI) -> None:
    """Make globex your org in hyperi's place."""
    app.state.org_registry.home.set(actor="test", name="globex")


def _new_account(*, groups: list[str], org: str) -> dict[str, Any]:
    """A create body for the account newbie."""
    return {
        "email": "newbie@example.test",
        "groups": groups,
        "name": "Newbie",
        "org": org,
        "password": PASSWORD,
        "username": "newbie",
    }


def _new_provider(*, domains: list[str], org: str) -> dict[str, Any]:
    """A create body for the provider acme-two."""
    return {
        "client_id": "acme-two-client",
        "issuer": "https://idp.acme.test",
        "name": "acme-two",
        "org": org,
        "trusted_email_domains": domains,
        "type": "generic",
    }


def _org_key(*, groups: list[str], org: str) -> dict[str, Any]:
    """A create body for the API key org-key."""
    return {"groups": groups, "name": "org-key", "org": org}


def _reach_globex_from_acme_viewers(*, app: FastAPI) -> None:
    """Give acme-viewers globex as another org it reaches."""
    app.state.group_store.update("acme-viewers", org_ids=["globex"])


LIVE_GRANTS_CASES: list[LiveGrantsCase] = [
    {
        "id": "an_org_viewer_reaches_its_own_org",
        "arrange": _leave_as_stored,
        "expected_org_ids": ["acme"],
        "expected_roles": ["org_viewer"],
        "username": "kim",
    },
    {
        "id": "a_system_group_listing_an_org_account_grants_it_nothing",
        "arrange": _add_kim_to_a_system_group,
        "expected_org_ids": ["acme"],
        "expected_roles": ["org_viewer"],
        "username": "kim",
    },
    {
        "id": "a_roleless_group_of_another_org_gives_it_nothing",
        "arrange": _add_kim_to_a_roleless_group_of_globex,
        "expected_org_ids": ["acme"],
        "expected_roles": ["org_viewer"],
        "username": "kim",
    },
    {
        "id": "a_system_org_viewer_naming_only_its_org_reaches_it",
        "arrange": _move_kim_to_a_system_viewer_of_acme,
        "expected_org_ids": ["acme"],
        "expected_roles": ["org_viewer"],
        "username": "kim",
    },
    {
        "id": "a_system_group_linked_to_its_idp_group_grants_it_nothing",
        "arrange": _link_a_system_group_to_ida,
        "expected_org_ids": ["acme"],
        "expected_roles": ["org_viewer"],
        "username": "ida",
    },
    {
        "id": "a_role_on_a_group_it_signs_in_through_grants_it_nothing",
        "arrange": _give_eng_a_role,
        "expected_org_ids": ["acme"],
        "expected_roles": ["org_viewer"],
        "username": "ida",
    },
    {
        "id": "a_system_group_its_idp_asserts_at_sign_in_grants_it_nothing",
        "arrange": _assert_a_system_group_for_ida,
        "expected_org_ids": ["acme"],
        "expected_roles": ["org_viewer"],
        "username": "ida",
    },
    {
        "id": "a_linked_sign_in_asserting_a_system_group_grants_it_nothing",
        "arrange": _link_a_sign_in_to_kim,
        "expected_org_ids": ["acme"],
        "expected_roles": ["org_viewer"],
        "username": "kim",
    },
    {
        "id": "its_own_group_reaching_another_org_grants_it_nothing",
        "arrange": _reach_globex_from_acme_viewers,
        "expected_org_ids": [],
        "expected_roles": [],
        "username": "kim",
    },
    {
        "id": "an_org_admin_reaches_its_own_org",
        "arrange": _make_kim_an_acme_admin,
        "expected_org_ids": ["acme"],
        "expected_roles": ["org_admin"],
        "username": "kim",
    },
    {
        "id": "a_system_group_naming_its_org_does_not_make_it_admin",
        "arrange": _add_kim_to_a_system_org_admin_group_of_acme,
        "expected_org_ids": ["acme"],
        "expected_roles": ["org_viewer"],
        "username": "kim",
    },
    {
        "id": "a_system_group_granting_org_admin_grants_your_orgs_account_nothing",
        "arrange": _add_ann_to_a_system_org_admin_group,
        "expected_org_ids": [],
        "expected_roles": ["data_analyst"],
        "username": "ann",
    },
    {
        "id": "an_account_of_your_org_holds_a_system_group",
        "arrange": _leave_as_stored,
        "expected_org_ids": [],
        "expected_roles": ["data_analyst"],
        "username": "ann",
    },
    {
        "id": "an_account_of_your_old_org_loses_its_system_group",
        "arrange": _name_globex_your_org,
        "expected_org_ids": [],
        "expected_roles": [],
        "username": "ann",
    },
]

ORG_ADMIN_ACCOUNTS_CASES: list[OrgAdminAccountsCase] = [
    {
        "id": "creating_an_account_in_its_org_passes",
        "body": _new_account(groups=["acme-viewers"], org="acme"),
        "expected_code": None,
        "expected_status": 201,
        "method": "POST",
        "path": ACCOUNTS,
    },
    {
        "id": "creating_an_account_in_another_org_is_refused",
        "body": _new_account(groups=["globex-viewers"], org="globex"),
        "expected_code": "forbidden",
        "expected_status": 403,
        "method": "POST",
        "path": ACCOUNTS,
    },
    {
        "id": "creating_a_root_account_is_refused",
        "body": _new_account(groups=["acme-viewers"], org=""),
        "expected_code": "forbidden",
        "expected_status": 403,
        "method": "POST",
        "path": ACCOUNTS,
    },
    {
        "id": "creating_an_account_with_a_role_it_does_not_hold_is_refused",
        "body": _new_account(groups=["acme-analysts"], org="acme"),
        "expected_code": "forbidden",
        "expected_status": 403,
        "method": "POST",
        "path": ACCOUNTS,
    },
    {
        "id": "reading_an_account_of_its_org_passes",
        "body": None,
        "expected_code": None,
        "expected_status": 200,
        "method": "GET",
        "path": f"{ACCOUNTS}/ace",
    },
    {
        "id": "another_orgs_account_is_not_found",
        "body": None,
        "expected_code": "not_found",
        "expected_status": 404,
        "method": "GET",
        "path": f"{ACCOUNTS}/gil",
    },
    {
        "id": "an_account_of_your_org_is_not_found",
        "body": None,
        "expected_code": "not_found",
        "expected_status": 404,
        "method": "GET",
        "path": f"{ACCOUNTS}/ann",
    },
    {
        "id": "making_an_account_of_its_org_admin_passes",
        "body": {"groups": ["acme-admins"]},
        "expected_code": None,
        "expected_status": 200,
        "method": "PUT",
        "path": f"{ACCOUNTS}/ace",
    },
    {
        "id": "disabling_an_account_of_its_org_passes",
        "body": {"enabled": False},
        "expected_code": None,
        "expected_status": 200,
        "method": "PUT",
        "path": f"{ACCOUNTS}/ace",
    },
    {
        "id": "moving_an_account_to_another_org_is_refused",
        "body": {"groups": ["globex-viewers"], "org": "globex"},
        "expected_code": "forbidden",
        "expected_status": 403,
        "method": "PUT",
        "path": f"{ACCOUNTS}/ace",
    },
    {
        "id": "editing_another_orgs_account_is_not_found",
        "body": {"enabled": False},
        "expected_code": "not_found",
        "expected_status": 404,
        "method": "PUT",
        "path": f"{ACCOUNTS}/gil",
    },
    {
        "id": "demoting_its_orgs_last_admin_is_refused",
        "body": {"groups": ["acme-viewers"]},
        "expected_code": "last_org_admin",
        "expected_status": 409,
        "method": "PUT",
        "path": f"{ACCOUNTS}/kim",
    },
    {
        "id": "resetting_a_password_in_its_org_passes",
        "body": {"new_password": "org-admin-reset-1"},
        "expected_code": None,
        "expected_status": 200,
        "method": "POST",
        "path": f"{ACCOUNTS}/ace/reset-password",
    },
    {
        "id": "resetting_another_orgs_password_is_not_found",
        "body": {"new_password": "org-admin-reset-1"},
        "expected_code": "not_found",
        "expected_status": 404,
        "method": "POST",
        "path": f"{ACCOUNTS}/gil/reset-password",
    },
    {
        "id": "deleting_an_account_of_its_org_passes",
        "body": None,
        "expected_code": None,
        "expected_status": 204,
        "method": "DELETE",
        "path": f"{ACCOUNTS}/ace",
    },
    {
        "id": "deleting_another_orgs_account_is_not_found",
        "body": None,
        "expected_code": "not_found",
        "expected_status": 404,
        "method": "DELETE",
        "path": f"{ACCOUNTS}/gil",
    },
]

ORG_ADMIN_PROVIDERS_CASES: list[OrgAdminProvidersCase] = [
    {
        "id": "creating_a_provider_in_its_org_passes",
        "body": _new_provider(domains=["acme.test"], org="acme"),
        "expected_code": None,
        "expected_status": 201,
        "method": "POST",
        "path": PROVIDERS,
    },
    {
        "id": "creating_a_provider_in_another_org_is_refused",
        "body": _new_provider(domains=[], org="globex"),
        "expected_code": "forbidden",
        "expected_status": 403,
        "method": "POST",
        "path": PROVIDERS,
    },
    {
        "id": "creating_a_platform_provider_is_refused",
        "body": _new_provider(domains=[], org=""),
        "expected_code": "forbidden",
        "expected_status": 403,
        "method": "POST",
        "path": PROVIDERS,
    },
    {
        "id": "trusting_another_orgs_domain_is_refused",
        "body": _new_provider(domains=["globex.test"], org="acme"),
        "expected_code": "domain_taken",
        "expected_status": 422,
        "method": "POST",
        "path": PROVIDERS,
    },
    {
        "id": "naming_its_provider_after_the_proxy_provider_is_refused",
        "body": {**_new_provider(domains=[], org="acme"), "name": "oidc"},
        "expected_code": "provider_name_reserved",
        "expected_status": 422,
        "method": "POST",
        "path": PROVIDERS,
    },
    {
        "id": "naming_its_provider_scim_is_refused",
        "body": {**_new_provider(domains=[], org="acme"), "name": "scim"},
        "expected_code": "provider_name_reserved",
        "expected_status": 422,
        "method": "POST",
        "path": PROVIDERS,
    },
    {
        "id": "reading_its_client_id_from_an_engine_env_var_is_refused",
        "body": {**_new_provider(domains=[], org="acme"), "client_id": "", "client_id_env": "PATH"},
        "expected_code": "env_source_refused",
        "expected_status": 422,
        "method": "POST",
        "path": PROVIDERS,
    },
    {
        "id": "reading_its_secret_from_an_engine_env_var_is_refused",
        "body": {**_new_provider(domains=[], org="acme"), "client_secret_env": "PATH"},
        "expected_code": "env_source_refused",
        "expected_status": 422,
        "method": "POST",
        "path": PROVIDERS,
    },
    {
        "id": "an_issuer_at_the_metadata_address_is_refused",
        "body": {**_new_provider(domains=[], org="acme"), "issuer": "http://169.254.169.254/"},
        "expected_code": "host_blocked",
        "expected_status": 422,
        "method": "POST",
        "path": PROVIDERS,
    },
    {
        "id": "an_issuer_on_the_engines_own_pod_is_refused",
        "body": {**_new_provider(domains=[], org="acme"), "issuer": "https://localhost:8443"},
        "expected_code": "host_blocked",
        "expected_status": 422,
        "method": "POST",
        "path": PROVIDERS,
    },
    {
        "id": "repointing_its_issuer_at_loopback_is_refused",
        "body": {"issuer": "http://127.0.0.1:8080"},
        "expected_code": "host_blocked",
        "expected_status": 422,
        "method": "PUT",
        "path": f"{PROVIDERS}/acme-idp",
    },
    {
        "id": "pointing_its_provider_at_an_engine_env_var_is_refused",
        "body": {"client_secret_env": "PATH"},
        "expected_code": "env_source_refused",
        "expected_status": 422,
        "method": "PUT",
        "path": f"{PROVIDERS}/acme-idp",
    },
    {
        "id": "reading_its_orgs_provider_passes",
        "body": None,
        "expected_code": None,
        "expected_status": 200,
        "method": "GET",
        "path": f"{PROVIDERS}/acme-idp",
    },
    {
        "id": "another_orgs_provider_is_not_found",
        "body": None,
        "expected_code": "not_found",
        "expected_status": 404,
        "method": "GET",
        "path": f"{PROVIDERS}/globex-idp",
    },
    {
        "id": "a_platform_provider_is_not_found",
        "body": None,
        "expected_code": "not_found",
        "expected_status": 404,
        "method": "GET",
        "path": f"{PROVIDERS}/platform-idp",
    },
    {
        "id": "editing_its_orgs_provider_passes",
        "body": {"display_name": "Acme sign-in"},
        "expected_code": None,
        "expected_status": 200,
        "method": "PUT",
        "path": f"{PROVIDERS}/acme-idp",
    },
    {
        "id": "moving_its_provider_to_another_org_is_refused",
        "body": {"org": "globex"},
        "expected_code": "forbidden",
        "expected_status": 403,
        "method": "PUT",
        "path": f"{PROVIDERS}/acme-idp",
    },
    {
        "id": "testing_another_orgs_provider_is_not_found",
        "body": None,
        "expected_code": "not_found",
        "expected_status": 404,
        "method": "GET",
        "path": f"{PROVIDERS}/globex-idp/test",
    },
    {
        "id": "verifying_another_orgs_provider_login_is_not_found",
        "body": None,
        "expected_code": "not_found",
        "expected_status": 404,
        "method": "GET",
        "path": f"{PROVIDERS}/globex-idp/verify-login",
    },
    {
        "id": "syncing_its_orgs_provider_groups_is_refused",
        "body": None,
        "expected_code": "forbidden",
        "expected_status": 403,
        "method": "POST",
        "path": f"{PROVIDERS}/acme-idp/sync",
    },
    {
        "id": "deleting_another_orgs_provider_is_not_found",
        "body": None,
        "expected_code": "not_found",
        "expected_status": 404,
        "method": "DELETE",
        "path": f"{PROVIDERS}/globex-idp",
    },
    {
        "id": "deleting_its_orgs_provider_passes",
        "body": None,
        "expected_code": None,
        "expected_status": 200,
        "method": "DELETE",
        "path": f"{PROVIDERS}/acme-idp",
    },
]

ORG_ADMIN_ROLE_CASES: list[OrgAdminRoleCase] = [
    {
        "id": "a_system_group_may_not_grant_org_admin",
        "body": {"name": "sys-org-admins", "roles": ["org_admin"]},
        "expected_code": "org_admin_org_only",
        "expected_status": 422,
        "method": "POST",
        "path": GROUPS,
    },
    {
        "id": "a_system_group_may_not_be_given_org_admin",
        "body": {"roles": ["org_admin"]},
        "expected_code": "org_admin_org_only",
        "expected_status": 422,
        "method": "PUT",
        "path": f"{GROUPS}/sys-analysts",
    },
    {
        "id": "a_created_group_may_not_grant_org_admin",
        "body": {"name": "acme-leads", "roles": ["org_admin"]},
        "expected_code": "org_admin_org_only",
        "expected_status": 422,
        "method": "POST",
        "path": GROUPS,
    },
]

ORG_ROLE_CASES: list[OrgRoleCase] = [
    {
        "id": "creating_an_org_account_with_its_viewer_group_passes",
        "body": _new_account(groups=["acme-viewers"], org="acme"),
        "expected_status": 201,
        "method": "POST",
        "path": ACCOUNTS,
    },
    {
        "id": "creating_an_org_account_with_another_orgs_group_is_refused",
        "body": _new_account(groups=["globex-viewers"], org="acme"),
        "expected_status": 422,
        "method": "POST",
        "path": ACCOUNTS,
    },
    {
        "id": "creating_an_org_account_with_a_system_group_is_refused",
        "body": _new_account(groups=["sys-analysts"], org="acme"),
        "expected_status": 422,
        "method": "POST",
        "path": ACCOUNTS,
    },
    {
        "id": "creating_an_org_account_with_its_orgs_other_role_is_refused",
        "body": _new_account(groups=["acme-analysts"], org="acme"),
        "expected_status": 422,
        "method": "POST",
        "path": ACCOUNTS,
    },
    {
        "id": "a_root_account_may_hold_any_group",
        "body": _new_account(groups=["globex-viewers", "sys-analysts"], org=""),
        "expected_status": 201,
        "method": "POST",
        "path": ACCOUNTS,
    },
    {
        "id": "an_account_of_your_org_may_hold_any_group",
        "body": _new_account(groups=["sys-analysts"], org="hyperi"),
        "expected_status": 201,
        "method": "POST",
        "path": ACCOUNTS,
    },
    {
        "id": "giving_an_org_account_another_orgs_group_is_refused",
        "body": {"groups": ["globex-viewers"]},
        "expected_status": 422,
        "method": "PUT",
        "path": f"{ACCOUNTS}/kim",
    },
    {
        "id": "making_an_org_account_its_orgs_admin_passes",
        "body": {"groups": ["acme-admins"]},
        "expected_status": 200,
        "method": "PUT",
        "path": f"{ACCOUNTS}/kim",
    },
    {
        "id": "moving_an_org_account_and_keeping_its_old_group_is_refused",
        "body": {"org": "globex"},
        "expected_status": 422,
        "method": "PUT",
        "path": f"{ACCOUNTS}/kim",
    },
    {
        "id": "moving_an_org_account_with_its_new_orgs_group_passes",
        "body": {"groups": ["globex-viewers"], "org": "globex"},
        "expected_status": 200,
        "method": "PUT",
        "path": f"{ACCOUNTS}/kim",
    },
    {
        "id": "adding_an_org_account_to_a_system_group_is_refused",
        "body": {"username": "kim"},
        "expected_status": 422,
        "method": "POST",
        "path": f"{GROUPS}/sys-analysts/members",
    },
    {
        "id": "giving_an_orgs_role_group_another_role_is_refused",
        "body": {"roles": ["data_analyst"]},
        "expected_status": 409,
        "method": "PUT",
        "path": f"{GROUPS}/acme-viewers",
    },
    {
        "id": "renaming_the_description_of_an_orgs_role_group_is_refused",
        "body": {"description": "renamed"},
        "expected_status": 409,
        "method": "PUT",
        "path": f"{GROUPS}/acme-admins",
    },
    {
        "id": "adding_a_member_to_an_orgs_role_group_through_the_group_is_refused",
        "body": {"username": "kim"},
        "expected_status": 409,
        "method": "POST",
        "path": f"{GROUPS}/globex-viewers/members",
    },
    {
        "id": "removing_a_member_from_an_orgs_role_group_through_the_group_is_refused",
        "body": None,
        "expected_status": 409,
        "method": "DELETE",
        "path": f"{GROUPS}/acme-viewers/members/kim",
    },
    {
        "id": "deleting_an_orgs_role_group_is_refused",
        "body": None,
        "expected_status": 409,
        "method": "DELETE",
        "path": f"{GROUPS}/acme-viewers",
    },
    {
        "id": "describing_a_group_that_is_no_orgs_role_group_passes",
        "body": {"description": "analysts"},
        "expected_status": 200,
        "method": "PUT",
        "path": f"{GROUPS}/sys-analysts",
    },
    {
        "id": "creating_a_system_group_holding_an_org_account_is_refused",
        "body": {"members": ["kim"], "name": "sys-kim", "roles": ["data_viewer"]},
        "expected_status": 422,
        "method": "POST",
        "path": GROUPS,
    },
    {
        "id": "creating_a_group_linked_to_an_org_accounts_idp_group_is_refused",
        "body": {
            "name": "new-analysts",
            "roles": ["data_analyst"],
            "source_id": "ext-1",
            "source_provider": "scim",
        },
        "expected_status": 422,
        "method": "POST",
        "path": GROUPS,
    },
    {
        "id": "linking_a_system_group_to_an_org_accounts_idp_group_is_refused",
        "body": {"source_id": "ext-1", "source_provider": "scim"},
        "expected_status": 422,
        "method": "PUT",
        "path": f"{GROUPS}/sys-analysts",
    },
    {
        "id": "linking_an_org_accounts_viewer_group_to_its_idp_group_passes",
        "body": {"source_id": "ext-1", "source_provider": "scim"},
        "expected_status": 200,
        "method": "PUT",
        "path": f"{GROUPS}/acme-viewers",
    },
    {
        "id": "giving_a_group_an_org_account_signs_in_through_a_role_is_refused",
        "body": {"roles": ["data_analyst"]},
        "expected_status": 422,
        "method": "PUT",
        "path": f"{GROUPS}/eng",
    },
    {
        "id": "moving_a_scim_external_id_to_an_org_accounts_idp_group_is_refused",
        "body": {
            "displayName": "sys-analysts",
            "externalId": "ext-1",
            "members": [],
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
        },
        "expected_status": 400,
        "method": "PUT",
        "path": f"{SCIM}/Groups/sys-analysts",
    },
    {
        "id": "minting_an_org_key_holding_a_system_group_is_refused",
        "body": _org_key(groups=["sys-analysts"], org="acme"),
        "expected_status": 422,
        "method": "POST",
        "path": API_KEYS,
    },
    {
        "id": "minting_an_org_key_holding_its_viewer_group_passes",
        "body": _org_key(groups=["acme-viewers"], org="acme"),
        "expected_status": 201,
        "method": "POST",
        "path": API_KEYS,
    },
    {
        "id": "minting_a_key_of_your_org_holding_a_system_group_passes",
        "body": _org_key(groups=["sys-analysts"], org="hyperi"),
        "expected_status": 201,
        "method": "POST",
        "path": API_KEYS,
    },
]
