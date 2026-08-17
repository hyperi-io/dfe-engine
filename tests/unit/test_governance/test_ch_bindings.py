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

from __future__ import annotations

from types import SimpleNamespace

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
        """org_viewer plus any platform role resolves platform-wards."""
        group = _group("odd", org_ids=["acme"], roles=["org_viewer", "data_viewer"])
        bindings = derive_group_bindings([group], [_org("acme")])
        assert [(b.group, b.org) for b in bindings] == [("odd", "")]

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
