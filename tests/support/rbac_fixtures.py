"""RBAC test fixtures - test the platform AS a role, without the plumbing.

A front-end dev should be able to write::

    def test_viewer_cannot_delete(client_as):
        resp = client_as("dfe-viewer").delete("/api/v1/sources/x")
        assert resp.status_code == 403

and never learn what a group claim, a source_id or an org scope is. That is what
this module is for.

The identities here MIRROR the shared OIDC test fixture
(hyperi-infra ``subprojects/dfe-oidc-testing/fixture.yaml``) so the fast in-process
path and the real dex/entra/okta e2e path assert the SAME users and groups. When
that fixture changes, update ``FIXTURE_GROUPS`` / ``FIXTURE_USERS`` to match - the
names are the contract, and drift between them is the bug this shared vocabulary
exists to prevent.

Group -> role mappings are the engine's own (``auth/resources/roles.yaml`` via
``rbac-vocabulary.md``); they are not invented here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.deps import create_access_token


@dataclass(frozen=True)
class FixtureGroup:
    roles: list[str] = field(default_factory=list)
    scope: str = "system"
    org_ids: list[str] = field(default_factory=list)


# The canonical groups, matching the shared fixture. dfe-admins / dfe-analysts /
# dfe-viewers / dfe-infra are the engine's own seeded defaults.
FIXTURE_GROUPS: dict[str, FixtureGroup] = {
    "dfe-admins": FixtureGroup(roles=["admin"]),
    "dfe-infra": FixtureGroup(roles=["infra_admin"]),
    "dfe-infra-viewers": FixtureGroup(roles=["infra_viewer"]),
    "dfe-analysts": FixtureGroup(roles=["data_analyst"]),
    "dfe-analyst-viewers": FixtureGroup(roles=["data_analyst_viewer"]),
    "dfe-viewers": FixtureGroup(roles=["data_viewer"]),
    "dfe-operators": FixtureGroup(roles=["dfe_operator"]),
    "dfe-test-org-viewers": FixtureGroup(
        roles=["customer_viewer"], scope="org:test_org", org_ids=["test_org"]
    ),
    "dfe-multi-viewers": FixtureGroup(
        roles=["customer_viewer"], org_ids=["test_org", "test_org_2"]
    ),
}

# The canonical users, named for the role they exercise.
FIXTURE_USERS: dict[str, list[str]] = {
    "dfe-admin": ["dfe-admins"],
    "dfe-infra-admin": ["dfe-infra"],
    "dfe-infra-viewer": ["dfe-infra-viewers"],
    "dfe-analyst": ["dfe-analysts"],
    "dfe-analyst-viewer": ["dfe-analyst-viewers"],
    "dfe-viewer": ["dfe-viewers"],
    "dfe-operator": ["dfe-operators"],
    "dfe-test-org-viewer": ["dfe-test-org-viewers"],
    "dfe-multi-viewer": ["dfe-multi-viewers"],
    "dfe-nobody": [],  # default deny - the one people forget
}


def seed_fixture_rbac(app) -> None:
    """Create the canonical groups + users in a running app's stores.

    Idempotent: existing groups/users are updated, not duplicated. Roles are
    resolved live from group membership, so this is all the engine needs to
    authenticate any fixture identity to the right role and org scope.
    """
    group_store = app.state.group_store
    account_store = app.state.account_store

    for name, spec in FIXTURE_GROUPS.items():
        if group_store.get(name) is None:
            group_store.create(name, roles=spec.roles, description="rbac fixture")
        group_store.update(name, roles=spec.roles, scope=spec.scope, org_ids=spec.org_ids)

    for username, groups in FIXTURE_USERS.items():
        if account_store.get(username) is None:
            account_store.create(username, f"{username}-pw", groups=groups)
        for gname in groups:
            group_store.add_member(gname, username)


@pytest.fixture
def client_as(client: TestClient, app, api_settings) -> Callable[[str], TestClient]:
    """Return a factory: ``client_as("dfe-viewer")`` -> an authed TestClient.

    The returned client carries a valid engine token for that fixture identity;
    roles and org scope resolve live from group membership, exactly as a real
    OIDC login would. Unknown names raise, so a typo fails loudly rather than
    silently authenticating as nobody.

    Depends on the ``client`` fixture so the app lifespan has run and
    ``app.state`` (the auth stores) is populated before we seed into it.
    """
    seed_fixture_rbac(app)

    def _make(identity: str) -> TestClient:
        if identity not in FIXTURE_USERS:
            raise KeyError(
                f"unknown fixture identity {identity!r}; known: {', '.join(sorted(FIXTURE_USERS))}"
            )
        token = create_access_token(
            data={"sub": identity, "org_id": "test_org"},
            settings=api_settings,
        )
        client = TestClient(app, raise_server_exceptions=False)
        client.headers.update({"Authorization": f"Bearer {token}"})
        return client

    return _make
