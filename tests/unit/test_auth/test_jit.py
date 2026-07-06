#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_jit.py
#  Purpose:      Unit tests for JitProvisioner shadow account creation
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import pytest

from dfe_engine.auth.accounts import AccountStore
from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.jit import JitProvisioner


@pytest.fixture
def stores(tmp_path):
    accounts = AccountStore(tmp_path / "accounts")
    groups = GroupStore(tmp_path / "groups")
    groups.create("acme-viewers", roles=["org_analyst"])
    groups.update("acme-viewers", org_ids=["acme"])
    groups.create("dfe-admins", roles=["admin"])
    groups.create("dfe-analysts", roles=["data_analyst"])
    return accounts, groups


class TestSanitiseUsername:
    def test_email(self):
        assert JitProvisioner.sanitise_username("jane@corp.com") == "jane-corp-com"

    def test_complex_email(self):
        assert (
            JitProvisioner.sanitise_username("user.name+tag@example.co.uk")
            == "user-name-tag-example-co-uk"
        )

    def test_already_safe(self):
        assert JitProvisioner.sanitise_username("simple-user") == "simple-user"


class TestAccountKey:
    def test_same_subject_is_stable(self):
        """The SAME subject must always map to the SAME account key."""
        assert JitProvisioner.account_key("alice@example.com") == JitProvisioner.account_key(
            "alice@example.com"
        )

    def test_key_starts_with_readable_slug(self):
        key = JitProvisioner.account_key("jane@corp.com")
        assert key.startswith("jane-corp-com-")

    def test_colliding_slugs_get_distinct_keys(self):
        """Subjects that share a slug must NOT share an account key."""
        subjects = ["alice@example.com", "alice.example.com", "Alice_example~com"]
        slugs = {JitProvisioner.sanitise_username(s) for s in subjects}
        keys = {JitProvisioner.account_key(s) for s in subjects}
        assert len(slugs) == 1  # slug alone collides all three
        assert len(keys) == 3  # key disambiguates by hashing the raw subject


class TestEnsureAccount:
    def test_first_login_creates_account(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        account = jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")
        assert account is not None
        assert account.external is True
        assert account.source_provider == "entra"
        # Shadow accounts have a bcrypt hash of "" — not a real credential
        assert account.password_hash.startswith("$2b$")
        assert account.last_login_at != ""

    def test_subsequent_login_monotonic_timestamp(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        key = JitProvisioner.account_key("jane@corp.com")
        jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")
        first = accounts.get(key)
        jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")
        second = accounts.get(key)
        # last_login_at never goes backwards (may stay equal — see FIX 7 no-op write).
        assert second.last_login_at >= first.last_login_at

    def test_groups_updated_on_change(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")
        jit.ensure_account("jane@corp.com", ["acme-viewers", "dfe-admins"], "entra")
        account = accounts.get(JitProvisioner.account_key("jane@corp.com"))
        assert "dfe-admins" in account.groups

    def test_race_condition_handled(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        # Pre-create the account under its real key (simulating a create race)
        accounts.create(JitProvisioner.account_key("race@user.com"), "", groups=["acme-viewers"])
        # Should not raise
        account = jit.ensure_account("race@user.com", ["acme-viewers"], "entra")
        assert account is not None

    def test_distinct_subjects_do_not_share_account(self, stores):
        """FIX 2: subjects that collapse to one slug must NOT share a shadow account.

        Before the fix all three collapse to 'alice-example-com' and clobber
        one account (and one enabled flag / group set).
        """
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        a1 = jit.ensure_account("alice@example.com", ["acme-viewers"], "entra")
        a2 = jit.ensure_account("alice.example.com", ["dfe-admins"], "entra")
        a3 = jit.ensure_account("Alice_example~com", [], "entra")

        # Three distinct accounts, not one.
        assert len({a1.username, a2.username, a3.username}) == 3
        # Re-presenting the SAME subject resolves the SAME account.
        again = jit.ensure_account("alice@example.com", ["acme-viewers"], "entra")
        assert again.username == a1.username
        # Group sets stayed independent (no cross-subject contamination).
        assert set(accounts.get(a1.username).groups) == {"acme-viewers"}
        assert set(accounts.get(a2.username).groups) == {"dfe-admins"}

    def test_unchanged_account_second_request_does_not_rewrite(self, stores):
        """FIX 7: back-to-back requests for an unchanged account do not each write."""
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")  # first login: create

        writes: list[str] = []
        original_update = accounts.update

        def counting_update(username, **fields):
            writes.append(username)
            return original_update(username, **fields)

        accounts.update = counting_update  # type: ignore[method-assign]
        jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")
        jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")
        # Unchanged groups + fresh last_login_at => zero rewrites (never two).
        assert writes == []


class TestEnsureAccountHdxInvite:
    def test_first_login_no_hdx_client_does_not_crash(self, stores):
        """ensure_account with no HyperDX client should succeed without error."""
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups, hyperdx_client=None)
        account = jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")
        assert account is not None

    def test_first_login_hdx_client_no_org_registry_does_not_crash(self, stores):
        """invite path skips gracefully when org_registry is None."""
        accounts, groups = stores

        class _FakeHdx:
            _connected = False

        jit = JitProvisioner(
            account_store=accounts,
            group_store=groups,
            hyperdx_client=_FakeHdx(),
            org_registry=None,
        )
        # Should not raise even with a fake (disconnected) hdx client
        account = jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")
        assert account is not None


def _file_secrets(tmp_path):
    """Real file-backed DfeSecrets (no mocks - same pattern as test_lifecycle.py)."""
    from dfe_engine.secrets import build_secrets
    from dfe_engine.settings import SecretsSettings

    return build_secrets(SecretsSettings(provider="file", path=str(tmp_path / "secrets")))


class TestResolveTeamApiKey:
    def test_returns_empty_without_org_registry(self, stores):
        _, groups = stores
        jit = JitProvisioner(account_store=None, group_store=groups, org_registry=None)
        assert jit._resolve_team_api_key("customer-acme") == ""

    def test_returns_empty_without_secrets_store(self, stores, tmp_path):
        """A recorded path with NO secrets store wired must fail soft, not raise."""
        accounts, groups = stores
        from dfe_engine.orgs.registry import OrgRegistry

        org_registry = OrgRegistry(tmp_path / "orgs")
        org_registry.create("acme", org_ids=["acme"])
        org_registry.update("acme", hyperdx_team_api_key_path="hyperdx/team-api-key/acme")

        jit = JitProvisioner(account_store=accounts, group_store=groups, org_registry=org_registry)
        assert jit._resolve_team_api_key("customer-acme") == ""

    def test_ga_shared_team_resolves_via_any_org_secret(self, stores, tmp_path):
        """GA team name (not customer-*) resolves its key via any org's stored path."""
        accounts, groups = stores
        from dfe_engine.orgs.registry import OrgRegistry

        secrets = _file_secrets(tmp_path)
        secrets.put("hyperdx/team-api-key/dfe", "shared-key")
        org_registry = OrgRegistry(tmp_path / "orgs")
        org_registry.create("acme", org_ids=["acme"])
        org_registry.update("acme", hyperdx_team_api_key_path="hyperdx/team-api-key/dfe")

        jit = JitProvisioner(
            account_store=accounts,
            group_store=groups,
            org_registry=org_registry,
            secrets_store=secrets,
        )
        assert jit._resolve_team_api_key("dfe") == "shared-key"

    def test_returns_empty_when_secret_missing(self, stores, tmp_path):
        accounts, groups = stores
        from dfe_engine.orgs.registry import OrgRegistry

        secrets = _file_secrets(tmp_path)
        org_registry = OrgRegistry(tmp_path / "orgs")
        org_registry.create("acme", org_ids=["acme"])
        org_registry.update("acme", hyperdx_team_api_key_path="hyperdx/team-api-key/acme")

        jit = JitProvisioner(
            account_store=accounts,
            group_store=groups,
            org_registry=org_registry,
            secrets_store=secrets,
        )
        # Secret never put -> empty string
        result = jit._resolve_team_api_key("customer-acme")
        assert result == ""

    def test_returns_key_when_secret_present(self, stores, tmp_path):
        accounts, groups = stores
        from dfe_engine.orgs.registry import OrgRegistry

        secrets = _file_secrets(tmp_path)
        secrets.put("hyperdx/team-api-key/acme", "team-secret-key")
        org_registry = OrgRegistry(tmp_path / "orgs")
        org_registry.create("acme", org_ids=["acme"])
        org_registry.update("acme", hyperdx_team_api_key_path="hyperdx/team-api-key/acme")

        jit = JitProvisioner(
            account_store=accounts,
            group_store=groups,
            org_registry=org_registry,
            secrets_store=secrets,
        )
        result = jit._resolve_team_api_key("customer-acme")
        assert result == "team-secret-key"


def _jit_with_roles(stores, **kwargs):
    """JitProvisioner wired with the built-in RoleConfig (config-driven HyperDX access)."""
    from dfe_engine.auth.roles import RoleConfig

    accounts, groups = stores
    return JitProvisioner(
        account_store=accounts,
        group_store=groups,
        role_config=RoleConfig.load_builtin(),
        **kwargs,
    )


class TestDomainGroup:
    """Task C: first-login domain-group auto-creation + managed-org association.

    external-OIDC login -> email domain -> `org_<domain>` group carrying the
    claiming org's org_ids + role org_analyst; the shadow account joins it.
    """

    def _env(self, tmp_path, *, claim: bool):
        from dfe_engine.orgs.registry import OrgRegistry

        accounts = AccountStore(tmp_path / "accounts")
        groups = GroupStore(tmp_path / "groups")
        orgs = OrgRegistry(tmp_path / "orgs")
        if claim:
            orgs.create("acme", org_ids=["acme-tenant", "acme-sub"], domains=["acme.com"])
        return accounts, groups, orgs

    def _jit(self, accounts, groups, orgs=None):
        return JitProvisioner(account_store=accounts, group_store=groups, org_registry=orgs)

    def test_claimed_domain_creates_group_with_org_ids_and_role(self, tmp_path):
        accounts, groups, orgs = self._env(tmp_path, claim=True)
        jit = self._jit(accounts, groups, orgs)
        jit.ensure_account("jane@acme.com", [], "oidc")

        group = groups.get("org_acme_com")
        assert group is not None
        assert group.roles == ["org_analyst"]
        assert group.org_ids == ["acme-tenant", "acme-sub"]
        # Claimed domain binds org_analyst at the owning org's scope (never system)
        # so its reads stay org-restricted per the tenant-scope design.
        assert group.scope == "org:acme"
        assert JitProvisioner.account_key("jane@acme.com") in group.members

    def test_unclaimed_domain_group_has_no_org_ids(self, tmp_path):
        accounts, groups, orgs = self._env(tmp_path, claim=True)
        jit = self._jit(accounts, groups, orgs)
        jit.ensure_account("bob@unclaimed.io", [], "oidc")

        group = groups.get("org_unclaimed_io")
        assert group is not None
        assert group.org_ids == []  # unclaimed -> no tenant access
        # Unclaimed domain gets NO role (system-scoped, empty) - the account exists
        # but has no grants until an org claims the domain or an admin assigns one.
        assert group.roles == []
        assert group.scope == "system"
        assert JitProvisioner.account_key("bob@unclaimed.io") in group.members

    def test_non_email_subject_skips_domain_group(self, tmp_path):
        accounts, groups, orgs = self._env(tmp_path, claim=True)
        jit = self._jit(accounts, groups, orgs)
        jit.ensure_account("opaque-uuid-subject-01", [], "oidc")
        # No `@` -> no domain -> no org_* group provisioned.
        assert [g for g in groups.list() if g.name.startswith("org_")] == []

    def test_second_user_same_domain_joins_existing_group(self, tmp_path):
        accounts, groups, orgs = self._env(tmp_path, claim=True)
        jit = self._jit(accounts, groups, orgs)
        jit.ensure_account("jane@acme.com", [], "oidc")
        jit.ensure_account("john@acme.com", [], "oidc")

        group = groups.get("org_acme_com")
        assert JitProvisioner.account_key("jane@acme.com") in group.members
        assert JitProvisioner.account_key("john@acme.com") in group.members
        # Group created once, not duplicated.
        assert [g.name for g in groups.list() if g.name == "org_acme_com"] == ["org_acme_com"]

    def test_repeat_login_writes_nothing_new(self, tmp_path):
        accounts, groups, orgs = self._env(tmp_path, claim=True)
        jit = self._jit(accounts, groups, orgs)
        jit.ensure_account("jane@acme.com", [], "oidc")  # first login: create group

        # Spy on group writes during the second (unchanged) login.
        writes: list[str] = []
        orig_create, orig_add = groups.create, groups.add_member

        def spy_create(*a, **k):
            writes.append("create")
            return orig_create(*a, **k)

        def spy_add(*a, **k):
            writes.append("add_member")
            return orig_add(*a, **k)

        groups.create = spy_create  # type: ignore[method-assign]
        groups.add_member = spy_add  # type: ignore[method-assign]
        jit.ensure_account("jane@acme.com", [], "oidc")  # repeat login
        assert writes == []

        # Member is present exactly once (idempotent).
        member = JitProvisioner.account_key("jane@acme.com")
        assert groups.get("org_acme_com").members.count(member) == 1

    def test_domain_claimed_later_propagates_on_relogin(self, tmp_path):
        # P2.20: a domain group created while unclaimed must pick up the org
        # binding when the org claims the domain later - on the next login, even
        # for a returning user.
        accounts, groups, orgs = self._env(tmp_path, claim=False)
        jit = self._jit(accounts, groups, orgs)
        jit.ensure_account("jane@acme.com", [], "oidc")  # first login: unclaimed
        grp = groups.get("org_acme_com")
        assert grp.org_ids == []
        assert grp.roles == []
        assert grp.scope == "system"

        # org now claims the domain
        orgs.create("acme", org_ids=["acme-tenant"], domains=["acme.com"])

        jit.ensure_account("jane@acme.com", [], "oidc")  # returning-user re-login
        grp = groups.get("org_acme_com")
        assert grp.org_ids == ["acme-tenant"]
        assert grp.roles == ["org_analyst"]
        assert grp.scope == "org:acme"

    def test_relogin_does_not_clobber_unclaimed_domain_group(self, tmp_path):
        # P2.20 must NOT downgrade an org_<domain> group to empty on re-login when
        # no org claims the domain - an admin may have configured it by hand.
        accounts, groups, orgs = self._env(tmp_path, claim=False)
        # admin pre-configures the domain group with a role, though acme.com is unclaimed
        groups.create("org_acme_com", roles=["org_analyst"], org_ids=["acme"], scope="org:acme")
        accounts.create(JitProvisioner.account_key("jane@acme.com"), "", groups=[])

        jit = self._jit(accounts, groups, orgs)
        jit.ensure_account("jane@acme.com", [], "oidc")  # returning-user re-login

        grp = groups.get("org_acme_com")
        assert grp.roles == ["org_analyst"]  # untouched, not wiped
        assert grp.org_ids == ["acme"]
        assert grp.scope == "org:acme"

    def test_no_org_registry_creates_group_without_org_ids(self, tmp_path):
        accounts = AccountStore(tmp_path / "accounts")
        groups = GroupStore(tmp_path / "groups")
        jit = JitProvisioner(account_store=accounts, group_store=groups, org_registry=None)
        jit.ensure_account("jane@acme.com", [], "oidc")

        group = groups.get("org_acme_com")
        assert group is not None
        assert group.org_ids == []  # no registry to resolve -> graceful, no org_ids

    def test_dotted_domain_maps_to_underscore_group_name(self, tmp_path):
        accounts, groups, orgs = self._env(tmp_path, claim=False)
        jit = self._jit(accounts, groups, orgs)
        jit.ensure_account("sam@sub.acme.co.uk", [], "oidc")
        assert groups.get("org_sub_acme_co_uk") is not None


class TestResolveHyperdxTeam:
    """Task D: team resolution consolidated onto DFE_HYPERDX_PER_GROUP + effective_hyperdx."""

    def test_ga_default_returns_shared_team(self, stores):
        # GA posture (per_group defaults False): everyone with access joins the one team.
        jit = _jit_with_roles(stores, ga_team_name="dfe")
        assert jit.resolve_hyperdx_team(["dfe-admins"]) == "dfe"  # admin -> full access
        assert jit.resolve_hyperdx_team(["acme-viewers"]) == "dfe"  # org_analyst -> org-scoped

    def test_per_group_returns_org_team(self, stores):
        jit = _jit_with_roles(stores, per_group=True)
        assert jit.resolve_hyperdx_team(["acme-viewers"]) == "customer-acme"

    def test_per_group_no_org_returns_empty(self, stores):
        # dfe-admins grants full access but carries no org_ids -> no org team under per-group.
        jit = _jit_with_roles(stores, per_group=True)
        assert jit.resolve_hyperdx_team(["dfe-admins"]) == ""

    def test_no_groups_returns_empty(self, stores):
        jit = _jit_with_roles(stores)
        assert jit.resolve_hyperdx_team([]) == ""

    def test_unknown_group_returns_empty(self, stores):
        jit = _jit_with_roles(stores)
        assert jit.resolve_hyperdx_team(["nonexistent-group"]) == ""


class TestHyperdxScopeGate:
    """Task C: only provision (resolve a team) for a principal whose effective HyperDX
    access is not `none`."""

    def _role_config(self):
        from dfe_engine.auth.roles import HyperdxAccess, RoleConfig, RoleDefinition

        return RoleConfig(
            {
                # No hyperdx block -> effective access is `none`.
                "no_hdx": RoleDefinition(description="no hyperdx", permissions=["query:read"]),
                "full_hdx": RoleDefinition(
                    description="full",
                    permissions=["query:read"],
                    hyperdx=HyperdxAccess(access="full", tenant_scoped=False),
                ),
            }
        )

    def test_access_none_role_gets_no_team(self, tmp_path):
        accounts = AccountStore(tmp_path / "accounts")
        groups = GroupStore(tmp_path / "groups")
        groups.create("plain", roles=["no_hdx"])
        groups.update("plain", org_ids=["acme"])  # has an org, but no HyperDX access

        jit = JitProvisioner(
            account_store=accounts,
            group_store=groups,
            role_config=self._role_config(),
            ga_team_name="dfe",
        )
        # access none -> no team, no HyperDX user (dfe-ui shows no link).
        assert jit.resolve_hyperdx_team(["plain"]) == ""

    def test_access_full_role_gets_team(self, tmp_path):
        accounts = AccountStore(tmp_path / "accounts")
        groups = GroupStore(tmp_path / "groups")
        groups.create("power", roles=["full_hdx"])

        jit = JitProvisioner(
            account_store=accounts,
            group_store=groups,
            role_config=self._role_config(),
            ga_team_name="dfe",
        )
        assert jit.resolve_hyperdx_team(["power"]) == "dfe"

    def test_per_group_none_access_beats_org_resolution(self, tmp_path):
        """The access gate wins even when an org WOULD resolve under per-group."""
        accounts = AccountStore(tmp_path / "accounts")
        groups = GroupStore(tmp_path / "groups")
        groups.create("plain", roles=["no_hdx"])
        groups.update("plain", org_ids=["acme"])

        jit = JitProvisioner(
            account_store=accounts,
            group_store=groups,
            role_config=self._role_config(),
            per_group=True,
        )
        assert jit.resolve_hyperdx_team(["plain"]) == ""
