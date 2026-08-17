#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_rbac_perspective.py
#  Purpose:      Exercise the platform AS each RBAC role, not just that OIDC works
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Role-perspective tests - drive real endpoints AS each fixture identity.

Proving a token carries the right role string is not enough; these assert the
platform actually ALLOWS and DENIES the right actions for each role, which is
what a UI or CLI user experiences. They use the shared ``client_as`` helper, so
they double as the worked example a front-end dev copies.

The identities and org ids mirror the shared OIDC fixture
(hyperi-infra ``subprojects/dfe-oidc-testing/fixture.yaml``); ``test_org`` is the
standard org id every consumer uses, ``test_org_2`` only exists for the two-org
boundary case. Role -> scope mappings are the engine's own
(``auth/resources/roles.yaml``) - see the mapping asserted in
``TestRoleResolution`` below.

Endpoints under test:
  GET  /api/v1/sources   -> needs source:read
  POST /api/v1/sources   -> needs source:write
  GET  /api/v1/auth/me   -> resolved roles + org_ids (no scope needed)
"""

from __future__ import annotations

import pytest

LIST = "/api/v1/sources"
ME = "/api/v1/auth/me"

# Each fixture identity resolves to exactly this role set, live from group
# membership. This is the contract the fixture, the IdP group claims, and the
# engine role config must all agree on - assert it directly so a drift in any
# of them fails here with a readable diff.
EXPECTED_ROLES: dict[str, list[str]] = {
    "dfe-admin": ["admin"],
    "dfe-infra-admin": ["infra_admin"],
    "dfe-infra-viewer": ["infra_viewer"],
    "dfe-analyst": ["data_analyst"],
    "dfe-analyst-viewer": ["data_analyst_viewer"],
    "dfe-viewer": ["data_viewer"],
    "dfe-operator": ["dfe_operator"],
    "dfe-test-org-viewer": ["org_viewer"],
    "dfe-multi-viewer": ["org_viewer"],
    "dfe-nobody": [],
}


class TestRoleResolution:
    """Every identity resolves to exactly the roles the fixture maps it to.

    Cheap, robust, and it documents the whole group -> role contract in one place.
    """

    @pytest.mark.parametrize(("identity", "roles"), sorted(EXPECTED_ROLES.items()))
    def test_identity_resolves_to_expected_roles(self, client_as, identity, roles):
        resp = client_as(identity).get(ME)
        assert resp.status_code == 200, f"{identity} could not read /auth/me"
        assert sorted(resp.json()["roles"]) == sorted(roles), (
            f"{identity} resolved to unexpected roles"
        )


class TestReadPerspective:
    """source:read - who can list sources.

    The infra family is the trap this guards: ``infra_admin`` is an "admin" by
    name but holds no data-plane scope at all (config/deploy/argo/org/group only),
    so it must be DENIED here exactly like the operator. Proving that stops the
    two planes (control vs data) being conflated.
    """

    @pytest.mark.parametrize(
        "identity",
        ["dfe-admin", "dfe-analyst", "dfe-analyst-viewer", "dfe-viewer"],
    )
    def test_roles_with_read_can_list(self, client_as, identity):
        resp = client_as(identity).get(LIST)
        assert resp.status_code == 200, f"{identity} should be able to list sources"

    @pytest.mark.parametrize(
        "identity",
        ["dfe-operator", "dfe-nobody", "dfe-infra-admin", "dfe-infra-viewer"],
    )
    def test_roles_without_read_are_denied(self, client_as, identity):
        # dfe_operator holds only governance:read + action:invoke:*; the infra
        # roles hold config/deployment/argo/org/group scopes but NO source:read;
        # nobody holds nothing. None of them has source:read.
        resp = client_as(identity).get(LIST)
        assert resp.status_code == 403, f"{identity} must NOT be able to list sources"


class TestWritePerspective:
    """source:write - the read/write split within and across role families."""

    @pytest.mark.parametrize("identity", ["dfe-admin", "dfe-analyst"])
    def test_roles_with_write_pass_authz(self, client_as, identity, sample_source):
        # admin holds *, data_analyst holds source:* - authorization passes (the
        # business result may be 201/409, never 403).
        resp = client_as(identity).post(LIST, json=sample_source)
        assert resp.status_code != 403, f"{identity} should pass source:write authz"

    @pytest.mark.parametrize(
        "identity",
        ["dfe-viewer", "dfe-analyst-viewer", "dfe-operator", "dfe-infra-admin", "dfe-nobody"],
    )
    def test_roles_without_write_are_denied(self, client_as, identity, sample_source):
        # read-only data roles, the operator, infra_admin (no data scope) and
        # nobody are all refused at authz.
        resp = client_as(identity).post(LIST, json=sample_source)
        assert resp.status_code == 403, f"{identity} must NOT be able to write sources"


class TestDataPlanePerspective:
    """org scope - a org viewer carries exactly their org_ids.

    This is the data plane (what you can SEE), orthogonal to the control plane
    above (what you can DO). ``test_org`` is the standard single org; the
    multi-viewer case proves a role held at SYSTEM scope still carries both orgs
    without over-granting.
    """

    def test_test_org_viewer_scoped_to_one_org(self, client_as):
        resp = client_as("dfe-test-org-viewer").get(ME)
        assert resp.status_code == 200
        assert resp.json()["org_ids"] == ["test_org"]

    def test_multi_viewer_carries_two_orgs(self, client_as):
        resp = client_as("dfe-multi-viewer").get(ME)
        assert resp.status_code == 200
        assert sorted(resp.json()["org_ids"]) == ["test_org", "test_org_2"]

    def test_nobody_has_no_orgs_and_no_roles(self, client_as):
        resp = client_as("dfe-nobody").get(ME)
        assert resp.status_code == 200
        data = resp.json()
        assert data["org_ids"] == []
        assert data["roles"] == []


def test_unknown_identity_raises(client_as):
    """A typo'd identity fails loudly rather than authenticating as nobody."""
    with pytest.raises(KeyError):
        client_as("dfe-typo")
