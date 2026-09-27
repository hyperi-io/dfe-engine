#  Project:      dfe-engine
#  File:         tests/unit/test_governance/test_ch_bindings.py
#  Purpose:      Group -> CH binding derivation, including both fail-closed paths
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Unit tests for deriving CH bindings from RBAC groups.

The two skip paths carry the security weight: a skipped group gets no ClickHouse
user, whereas a group emitted with no org role holds no restrictive policy and
therefore reads EVERY org's rows.
"""

from types import SimpleNamespace

import pytest

from dfe_engine.auth.models import Scope, ScopedGrant, platform_grants
from dfe_engine.governance.ch.bindings import derive_group_bindings


def _org(name: str, ids: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, org_ids=ids if ids is not None else [name])


def _group(
    name: str,
    *,
    scope: str = "system",
    org_ids: list[str] | None = None,
    roles: list[str] | None = None,
) -> SimpleNamespace:
    scope_org = scope[len("org:") :] if scope.startswith("org:") else ""
    return SimpleNamespace(name=name, scope_org=scope_org, org_ids=org_ids or [], roles=roles or [])


class TestOrgScopedGroups:
    def test_org_scoped_group_binds_to_its_owning_org(self):
        bindings = derive_group_bindings([_group("acme-ro", scope="org:acme")], [_org("acme")])
        assert [(b.group, b.org) for b in bindings] == [("acme-ro", "acme")]

    def test_org_ids_alone_bind_the_group(self):
        """A system group listing one org is still org-tied."""
        bindings = derive_group_bindings([_group("analysts", org_ids=["acme"])], [_org("acme")])
        assert [(b.group, b.org) for b in bindings] == [("analysts", "acme")]

    def test_scope_and_org_ids_agreeing_is_one_org(self):
        group = _group("acme-ro", scope="org:acme", org_ids=["acme"])
        bindings = derive_group_bindings([group], [_org("acme")])
        assert [(b.group, b.org) for b in bindings] == [("acme-ro", "acme")]

    def test_tier_is_left_for_the_reconciler_to_default(self):
        bindings = derive_group_bindings([_group("acme-ro", scope="org:acme")], [_org("acme")])
        assert bindings[0].tier == ""

    def test_user_name_follows_the_group(self):
        bindings = derive_group_bindings([_group("soc-ro", scope="org:acme")], [_org("acme")])
        assert bindings[0].user() == "dfe_grp_soc-ro"

    @pytest.mark.parametrize(
        "roles",
        [
            ["org_viewer", "dfe_operator"],
            ["org_viewer", "infra_admin"],
            ["org_viewer", "dfe_operator", "infra_admin"],
            ["org_viewer", "data_analyst"],
            ["data_analyst"],
            ["admin"],
        ],
    )
    def test_org_scoped_group_keeps_its_org_whatever_it_holds(self, roles):
        """Its roles bind at the org's scope, so none of them reach every org's rows."""
        group = _group("acme-team", scope="org:acme", roles=roles)
        bindings = derive_group_bindings([group], [_org("acme"), _org("beta")])
        assert [(b.group, b.org) for b in bindings] == [("acme-team", "acme")]

    @pytest.mark.parametrize("role", ["admin", "infra_admin"])
    def test_an_org_scoped_admin_group_reads_no_platform_telemetry(self, role):
        """The otel database holds every org's telemetry, so only a system grant reads it."""
        groups = [
            _group("acme-admins", scope="org:acme", roles=[role]),
            _group("platform-admins", roles=[role]),
        ]
        bindings = derive_group_bindings(groups, [_org("acme")])
        assert {b.group: b.ch_roles for b in bindings} == {
            "acme-admins": [],
            "platform-admins": ["otel_reader"],
        }


class TestUnrestrictedGroups:
    def test_group_claiming_no_org_is_unrestricted(self):
        """The platform team: no tenant role, so no row policy targets it."""
        bindings = derive_group_bindings([_group("platform")], [_org("acme")])
        assert [(b.group, b.org) for b in bindings] == [("platform", "")]

    def test_platform_role_beats_org_markers(self):
        """An analyst matched by a domain rule stays unrestricted - the org
        filter fences tenants in, never the platform's own people out."""
        group = _group("analysts", org_ids=["acme"], roles=["data_analyst"])
        bindings = derive_group_bindings([group], [_org("acme")])
        assert [(b.group, b.org) for b in bindings] == [("analysts", "")]

    def test_customer_role_alone_keeps_the_pin(self):
        group = _group("acme-view", org_ids=["acme"], roles=["org_viewer"])
        bindings = derive_group_bindings([group], [_org("acme")])
        assert [(b.group, b.org) for b in bindings] == [("acme-view", "acme")]

    def test_mixed_roles_go_unrestricted(self):
        """In a system group, org_viewer plus any platform role resolves platform-wards."""
        group = _group("odd", org_ids=["acme"], roles=["org_viewer", "data_viewer"])
        bindings = derive_group_bindings([group], [_org("acme")])
        assert [(b.group, b.org) for b in bindings] == [("odd", "")]

    @pytest.mark.parametrize(
        "role", ["admin", "data_analyst", "data_analyst_viewer", "data_viewer"]
    )
    def test_system_scope_data_role_stays_unrestricted(self, role):
        group = _group("platform-data", org_ids=["acme"], roles=["org_viewer", role])
        bindings = derive_group_bindings([group], [_org("acme")])
        assert [(b.group, b.org) for b in bindings] == [("platform-data", "")]

    def test_admin_groups_compose_the_otel_reader(self):
        """Admins and infra admins read platform telemetry; nobody else does."""
        groups = [
            _group("admins", roles=["admin"]),
            _group("infra", roles=["infra_admin"]),
            _group("analysts", roles=["data_analyst"]),
            _group("acme-view", org_ids=["acme"], roles=["org_viewer"]),
        ]
        bindings = derive_group_bindings(groups, [_org("acme")])
        assert {b.group: b.ch_roles for b in bindings} == {
            "admins": ["otel_reader"],
            "infra": ["otel_reader"],
            "analysts": [],
            "acme-view": [],
        }


class TestPlatformGrants:
    """The one filter both the bindings and the HyperDX connection read decide on."""

    def test_a_system_scope_platform_grant_reads_across_orgs(self):
        grant = ScopedGrant(role="data_analyst", scope=Scope())
        assert platform_grants([grant]) == [grant]

    def test_an_org_scoped_grant_never_does(self):
        acme = Scope(type="org", id="acme")
        grants = [ScopedGrant(role=role, scope=acme) for role in ("admin", "data_analyst")]
        assert platform_grants(grants) == []

    def test_org_viewer_never_does_even_at_system_scope(self):
        assert platform_grants([ScopedGrant(role="org_viewer", scope=Scope())]) == []

    def test_no_grants_is_none(self):
        assert platform_grants([]) == []


class TestFailsClosed:
    def test_group_resolving_to_several_orgs_is_skipped(self):
        """Two org roles AND their restrictive policies together -> zero rows.

        Skipping is the honest outcome: the group's intent cannot be expressed,
        and emitting it without an org role would read every org instead.
        """
        group = _group("multi", org_ids=["acme", "beta"])
        bindings = derive_group_bindings([group], [_org("acme"), _org("beta")])
        assert bindings == []

    def test_group_claiming_an_unregistered_org_is_skipped(self):
        """Otherwise the unmatched claim silently degrades to unrestricted."""
        bindings = derive_group_bindings([_group("ghost", org_ids=["nope"])], [_org("acme")])
        assert bindings == []

    def test_a_skipped_group_does_not_stop_the_others(self):
        groups = [
            _group("multi", org_ids=["acme", "beta"]),
            _group("acme-ro", scope="org:acme"),
        ]
        bindings = derive_group_bindings(groups, [_org("acme"), _org("beta")])
        assert [(b.group, b.org) for b in bindings] == [("acme-ro", "acme")]

    def test_org_scoped_group_whose_org_was_deleted_is_skipped(self):
        bindings = derive_group_bindings([_group("orphan", scope="org:gone")], [_org("acme")])
        assert bindings == []
