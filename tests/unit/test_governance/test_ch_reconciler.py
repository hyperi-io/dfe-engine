#  Project:      dfe-engine
#  File:         tests/unit/test_governance/test_ch_reconciler.py
#  Purpose:      Pure reconciler tests - render_all composition + stale-drop diff
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Unit tests for the pure parts of ChRbacReconciler (no cluster).

The apply path (execute + discovery against real CH) is covered by the real-CH
integration tests; here we prove the ordered DDL composition and the stale-object
diff, both of which are pure functions.
"""

from __future__ import annotations

from types import SimpleNamespace

from dfe_engine.governance.ch.models import ChServiceRole, ChTier, GroupChBinding
from dfe_engine.governance.ch.reconciler import (
    ChRbacReconciler,
    _default_tier_name,
    compute_drops,
)


def _org(name: str, ids: list[str]) -> SimpleNamespace:
    return SimpleNamespace(name=name, org_ids=ids)


def _rec() -> ChRbacReconciler:
    # render_all is pure - it never touches the client.
    return ChRbacReconciler(admin_client=None)


class TestDefaultTierName:
    def test_flagged_default_wins(self):
        tiers = [
            ChTier(name="a1", kind="analyst"),
            ChTier(name="a2", kind="analyst", default=True),
        ]
        assert _default_tier_name(tiers, "analyst") == "a2"

    def test_first_of_kind_when_no_default(self):
        tiers = [ChTier(name="a1", kind="analyst"), ChTier(name="a2", kind="analyst")]
        assert _default_tier_name(tiers, "analyst") == "a1"

    def test_empty_when_kind_absent(self):
        assert _default_tier_name([ChTier(name="a1", kind="analyst")], "hunt") == ""


class TestRenderAll:
    def _inputs(self):
        tiers = [
            ChTier(
                name="analyst_tier_2",
                kind="analyst",
                default=True,
                grants=["SELECT ON dfe.*"],
                settings={"readonly": 1},
            ),
            ChTier(name="hunt_tier_2", kind="hunt", default=True, grants=["INSERT ON dfe_hunts.*"]),
        ]
        service_roles = [ChServiceRole(name="loader", mint_user=True, grants=["INSERT ON dfe.*"])]
        orgs = [_org("acme", ["acme"])]
        bindings = [
            GroupChBinding(group="soc", org="acme"),  # org-scoped, default tier
            GroupChBinding(group="admin"),  # unrestricted, default tier
            GroupChBinding(group="nohash"),  # no minted secret -> skipped
        ]
        return tiers, service_roles, orgs, bindings

    def test_full_composition_and_order(self):
        tiers, service_roles, orgs, bindings = self._inputs()
        stmts = _rec().render_all(
            tiers=tiers,
            service_roles=service_roles,
            orgs=orgs,
            bindings=bindings,
            org_tables=[("dfe", "events")],
            service_hashes={"loader": "svchash"},
            group_hashes={"soc": "sochash", "admin": "adminhash"},  # 'nohash' absent
        )
        s = "\n".join(stmts)
        # ordering: tier role -> org role -> group user
        assert s.index("dfe_analyst_tier_2_role") < s.index("dfe_org_acme_role")
        assert s.index("dfe_org_acme_role") < s.index("dfe_grp_soc")
        # minted service user rendered
        assert "CREATE USER IF NOT EXISTS `dfe_loader`" in s
        assert "GRANT `dfe_loader_role` TO `dfe_loader`" in s
        # org-scoped user: default analyst tier + org role
        assert "GRANT `dfe_analyst_tier_2_role` TO `dfe_grp_soc`" in s
        assert "GRANT `dfe_org_acme_role` TO `dfe_grp_soc`" in s
        # unrestricted user: tier only, no org grant
        assert "GRANT `dfe_analyst_tier_2_role` TO `dfe_grp_admin`" in s
        assert "GRANT `dfe_org_acme_role` TO `dfe_grp_admin`" not in s
        # binding without a minted secret is skipped
        assert "dfe_grp_nohash" not in s

    def test_explicit_tier_overrides_default(self):
        tiers, _service, _orgs, _b = self._inputs()
        b = [GroupChBinding(group="hunter", tier="hunt_tier_2")]
        stmts = _rec().render_all(
            tiers=tiers,
            service_roles=[],
            orgs=[],
            bindings=b,
            org_tables=[],
            service_hashes={},
            group_hashes={"hunter": "h"},
        )
        assert "GRANT `dfe_hunt_tier_2_role` TO `dfe_grp_hunter`" in "\n".join(stmts)

    def test_org_role_skipped_if_org_unknown(self):
        tiers, _service, _orgs, _b = self._inputs()
        b = [GroupChBinding(group="x", org="ghost")]  # no Org 'ghost' exists
        stmts = _rec().render_all(
            tiers=tiers,
            service_roles=[],
            orgs=[],
            bindings=b,
            org_tables=[],
            service_hashes={},
            group_hashes={"x": "h"},
        )
        assert "dfe_org_ghost_role" not in "\n".join(stmts)

    def test_non_mint_service_role_no_user(self):
        r = [ChServiceRole(name="query_reader", grants=["SELECT ON dfe.*"])]
        stmts = _rec().render_all(
            tiers=[],
            service_roles=r,
            orgs=[],
            bindings=[],
            org_tables=[],
            service_hashes={},
            group_hashes={},
        )
        s = "\n".join(stmts)
        assert "CREATE ROLE IF NOT EXISTS `dfe_query_reader_role`" in s
        assert "CREATE USER" not in s


class TestComputeDrops:
    def test_stale_role_and_policy_dropped_policies_first(self):
        existing_roles = {"dfe_org_acme_role", "dfe_org_gone_role"}
        existing_policies = [
            ("dfe_rowpol_acme_dfe_events", "dfe", "events"),
            ("dfe_rowpol_gone_dfe_events", "dfe", "events"),
        ]
        orgs = [_org("acme", ["acme"])]
        drops = compute_drops(existing_roles, existing_policies, orgs, [("dfe", "events")])
        assert any("DROP ROW POLICY IF EXISTS `dfe_rowpol_gone_dfe_events`" in d for d in drops)
        assert any("DROP ROLE IF EXISTS `dfe_org_gone_role`" in d for d in drops)
        assert all("acme" not in d for d in drops)  # the live org is untouched
        # policies drop before roles (a policy targets a role)
        pol_i = next(i for i, d in enumerate(drops) if "ROW POLICY" in d)
        role_i = next(i for i, d in enumerate(drops) if "DROP ROLE" in d)
        assert pol_i < role_i

    def test_no_drops_when_all_desired(self):
        existing_roles = {"dfe_org_acme_role"}
        existing_policies = [("dfe_rowpol_acme_dfe_events", "dfe", "events")]
        orgs = [_org("acme", ["acme"])]
        assert compute_drops(existing_roles, existing_policies, orgs, [("dfe", "events")]) == []

    def test_ignores_non_dfe_objects(self):
        existing_roles = {"some_other_role", "dfe_org_gone_role"}
        existing_policies = [("handmade_policy", "dfe", "events")]
        drops = compute_drops(existing_roles, existing_policies, [], [])
        assert all("some_other_role" not in d for d in drops)
        assert all("handmade_policy" not in d for d in drops)
        assert any("dfe_org_gone_role" in d for d in drops)
