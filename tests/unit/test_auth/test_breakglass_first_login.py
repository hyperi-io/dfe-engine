#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_breakglass_first_login.py
#  Purpose:      First-login model - posture gate, two-account seed, break-glass hash
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The engine half of the first-login model (dfe-engine#298).

Every password here is generated at import. ``changeme`` is the one literal, and
only where it is the subject: it is the string the gate refuses. A test that
hardcodes a passing password reads as a credential to a secret scanner and to
whoever copies the fixture next.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from dfe_engine.auth import admin_retirement, breakglass
from dfe_engine.auth.bootstrap import (
    DefaultCredentialsError,
    bootstrap_auth,
    default_credentials_in_use,
    require_admin_password,
)
from dfe_engine.auth.deployment_hints import (
    DOCKER,
    KUBERNETES,
    LOCAL,
    credential_fetch_command,
    detect_deploy_kind,
    rotation_store_command,
)
from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.registry import default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.settings import SeedAccount

MINTED_ADMIN = secrets.token_urlsafe(16)
MINTED_BREAKGLASS = secrets.token_urlsafe(16)
MINTED_SEED = secrets.token_urlsafe(16)


@pytest.fixture
def crud(tmp_path: Path) -> GitCrud:
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    return GitCrud(repo, default_registry())


# ── The posture gate ─────────────────────────────────────────


class TestPostureGate:
    @pytest.mark.parametrize("env", ["production", "prod", "staging", ""])
    def test_default_password_refuses_outside_a_dev_posture(self, env):
        with pytest.raises(DefaultCredentialsError) as exc:
            require_admin_password("changeme", env)
        assert "DFE_AUTH_LOCAL_ADMIN_PASSWORD" in str(exc.value)
        assert "DFE_ENV" in str(exc.value)

    @pytest.mark.parametrize("env", ["production", "prod", "staging"])
    def test_unset_password_refuses_outside_a_dev_posture(self, env):
        with pytest.raises(DefaultCredentialsError):
            require_admin_password("", env)

    @pytest.mark.parametrize("env", ["dev", "development", "local", "test", "ci"])
    def test_dev_posture_runs_on_the_default_and_flags_it(self, env):
        assert require_admin_password("changeme", env) is True
        assert require_admin_password("", env) is True

    @pytest.mark.parametrize("env", ["production", "dev"])
    def test_a_minted_password_passes_in_any_posture(self, env):
        assert require_admin_password(MINTED_ADMIN, env) is False

    @pytest.mark.parametrize("env", ["production", "dev"])
    def test_a_retired_admin_starts_with_no_password_at_all(self, env):
        """The point of retiring: the operator deletes the injected credential."""
        assert require_admin_password("", env, retired=True) is False

    def test_default_credentials_predicate(self):
        assert default_credentials_in_use("") is True
        assert default_credentials_in_use("changeme") is True
        assert default_credentials_in_use(MINTED_ADMIN) is False

    @pytest.mark.parametrize("padded", ["changeme\n", " changeme", "changeme \t", "   ", "\n"])
    def test_the_predicate_strips_before_comparing(self, padded):
        """A Secret, a heredoc or an .env line all deliver the default with whitespace on it."""
        assert default_credentials_in_use(padded) is True
        with pytest.raises(DefaultCredentialsError):
            require_admin_password(padded, "production")


# ── One seed path, reconciled on every boot ──────────────────


class TestAdminSeed:
    def test_admin_password_reconciles_on_every_boot(self, tmp_path: Path):
        auth_dir = tmp_path / "auth"
        bootstrap_auth(auth_dir, default_admin_password=MINTED_ADMIN)

        # A rebuild with a rotated config password: config wins, as for a seed account.
        rotated = MINTED_ADMIN + "-rotated"
        store, *_ = bootstrap_auth(auth_dir, default_admin_password=rotated)

        assert store.verify_password("admin", rotated)
        assert not store.verify_password("admin", MINTED_ADMIN)

    def test_a_store_side_change_is_reverted_at_the_next_boot(self, tmp_path: Path):
        auth_dir = tmp_path / "auth"
        store, *_ = bootstrap_auth(auth_dir, default_admin_password=MINTED_ADMIN)
        store.reset_password("admin", MINTED_ADMIN + "-typed-into-the-ui")

        store, *_ = bootstrap_auth(auth_dir, default_admin_password=MINTED_ADMIN)

        assert store.verify_password("admin", MINTED_ADMIN)

    def test_admin_and_named_seeds_share_the_one_path(self, tmp_path: Path):
        seeds = [SeedAccount(username="kay", password=MINTED_SEED, groups=["dfe-viewers"])]

        store, groups, *_ = bootstrap_auth(
            tmp_path / "auth", default_admin_password=MINTED_ADMIN, seed_accounts=seeds
        )

        assert store.verify_password("admin", MINTED_ADMIN)
        assert store.verify_password("kay", MINTED_SEED)
        assert "admin" in groups.get("dfe-admins").members


# ── The break-glass hash, round-tripped through gitcrud ──────


class TestBreakGlassHash:
    def test_hash_is_minted_into_gitcrud_and_seeds_the_account(self, tmp_path: Path, crud):
        store, groups, *_ = bootstrap_auth(
            tmp_path / "auth",
            default_admin_password=MINTED_ADMIN,
            gitcrud=crud,
            breakglass_password=MINTED_BREAKGLASS,
        )

        committed = crud.get(breakglass.CLASS, breakglass.NAME)["breakglass"]["password_hash"]
        assert committed.startswith("$2")
        assert store.verify_password(breakglass.USERNAME, MINTED_BREAKGLASS)
        assert breakglass.USERNAME in groups.get(breakglass.GROUP).members

    def test_the_committed_hash_survives_a_rebuilt_store(self, tmp_path: Path, crud):
        bootstrap_auth(
            tmp_path / "auth",
            default_admin_password=MINTED_ADMIN,
            gitcrud=crud,
            breakglass_password=MINTED_BREAKGLASS,
        )

        # A rebuilt pod: empty auth dir, same deploy repo, no configured password.
        store, *_ = bootstrap_auth(
            tmp_path / "rebuilt-auth", default_admin_password=MINTED_ADMIN, gitcrud=crud
        )

        assert store.verify_password(breakglass.USERNAME, MINTED_BREAKGLASS)

    def test_a_configured_password_is_ignored_once_a_hash_exists(self, tmp_path: Path, crud):
        bootstrap_auth(
            tmp_path / "auth",
            default_admin_password=MINTED_ADMIN,
            gitcrud=crud,
            breakglass_password=MINTED_BREAKGLASS,
        )

        store, *_ = bootstrap_auth(
            tmp_path / "auth",
            default_admin_password=MINTED_ADMIN,
            gitcrud=crud,
            breakglass_password=MINTED_BREAKGLASS + "-a-later-edit",
        )

        assert store.verify_password(breakglass.USERNAME, MINTED_BREAKGLASS)
        assert not store.verify_password(breakglass.USERNAME, MINTED_BREAKGLASS + "-a-later-edit")

    def test_no_gitcrud_means_no_break_glass_account(self, tmp_path: Path):
        store, *_ = bootstrap_auth(
            tmp_path / "auth",
            default_admin_password=MINTED_ADMIN,
            breakglass_password=MINTED_BREAKGLASS,
        )

        assert store.get(breakglass.USERNAME) is None

    def test_enabled_defaults_true_and_round_trips(self, crud):
        assert breakglass.is_enabled(crud) is True

        breakglass.set_enabled(crud, False, "tester")
        assert breakglass.is_enabled(crud) is False

        breakglass.set_enabled(crud, True, "tester")
        assert breakglass.is_enabled(crud) is True

    def test_an_unreadable_settings_file_leaves_recovery_open(self, crud):
        path = crud.repo_path / "governance" / "settings" / "auth.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{{ not: yaml\n")

        assert breakglass.is_enabled(crud) is True
        assert breakglass.stored_hash(crud) == ""


# ── Retiring the bootstrap admin ─────────────────────────────


class TestRetireTheBootstrapAdmin:
    def test_the_fact_round_trips_through_the_deploy_repo(self, crud):
        assert admin_retirement.is_retired(crud) is False

        admin_retirement.set_retired(crud, "kaz")

        assert admin_retirement.is_retired(crud) is True
        assert crud.get(admin_retirement.CLASS, admin_retirement.NAME)["admin_retired"] is True

    def test_the_fact_shares_the_file_with_the_break_glass_hash(self, tmp_path: Path, crud):
        bootstrap_auth(
            tmp_path / "auth",
            default_admin_password=MINTED_ADMIN,
            gitcrud=crud,
            breakglass_password=MINTED_BREAKGLASS,
        )

        admin_retirement.set_retired(crud, "kaz")

        doc = crud.get(breakglass.CLASS, breakglass.NAME)
        assert doc["admin_retired"] is True
        assert doc["breakglass"]["password_hash"].startswith("$2")

    def test_no_deploy_repo_means_no_retirement(self):
        assert admin_retirement.is_retired(None) is False

    def test_an_unreadable_settings_file_reads_as_not_retired(self, crud):
        path = crud.repo_path / "governance" / "settings" / "auth.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{{ not: yaml\n")

        assert admin_retirement.is_retired(crud) is False

    def test_the_seed_is_skipped_and_the_account_disabled(self, tmp_path: Path, crud):
        auth_dir = tmp_path / "auth"
        store, *_ = bootstrap_auth(auth_dir, default_admin_password=MINTED_ADMIN, gitcrud=crud)
        assert store.get("admin").enabled is True

        admin_retirement.set_retired(crud, "kaz")
        store, *_ = bootstrap_auth(auth_dir, default_admin_password=MINTED_ADMIN, gitcrud=crud)

        # The hash is left alone -- the disabled account is what refuses the login.
        assert store.get("admin").enabled is False

    def test_a_rebuilt_store_does_not_bring_the_admin_back(self, tmp_path: Path, crud):
        bootstrap_auth(tmp_path / "auth", default_admin_password=MINTED_ADMIN, gitcrud=crud)
        admin_retirement.set_retired(crud, "kaz")

        # A rebuilt pod: empty auth dir, same deploy repo, no configured password.
        store, *_ = bootstrap_auth(tmp_path / "rebuilt-auth", gitcrud=crud)

        admin = store.get("admin")
        assert admin is None or admin.enabled is False

    def test_named_seed_accounts_still_reconcile(self, tmp_path: Path, crud):
        """Retirement is about the bootstrap admin, not the deployment's own seeds."""
        admin_retirement.set_retired(crud, "kaz")
        seeds = [SeedAccount(username="kay", password=MINTED_SEED, groups=["dfe-viewers"])]

        store, *_ = bootstrap_auth(tmp_path / "auth", gitcrud=crud, seed_accounts=seeds)

        assert store.verify_password("kay", MINTED_SEED)


class TestAnotherAdminExists:
    """The one predicate the API refuses on and the wizard enables its button from."""

    def _stores(self, tmp_path: Path, crud):
        store, groups, *_ = bootstrap_auth(
            tmp_path / "auth",
            default_admin_password=MINTED_ADMIN,
            gitcrud=crud,
            breakglass_password=MINTED_BREAKGLASS,
        )
        return store, groups

    def test_the_seeded_pair_does_not_count(self, tmp_path: Path, crud):
        store, groups = self._stores(tmp_path, crud)

        assert admin_retirement.another_admin_exists(store, groups, "admin") is False

    def test_an_enabled_admin_group_member_counts(self, tmp_path: Path, crud):
        store, groups = self._stores(tmp_path, crud)
        store.create("alice", MINTED_SEED, groups=["dfe-admins"])

        assert admin_retirement.another_admin_exists(store, groups, "admin") is True

    def test_a_disabled_account_does_not_count(self, tmp_path: Path, crud):
        store, groups = self._stores(tmp_path, crud)
        store.create("alice", MINTED_SEED, groups=["dfe-admins"])
        store.update("alice", enabled=False)

        assert admin_retirement.another_admin_exists(store, groups, "admin") is False

    def test_a_user_without_the_admin_role_does_not_count(self, tmp_path: Path, crud):
        store, groups = self._stores(tmp_path, crud)
        store.create("bob", MINTED_SEED, groups=["dfe-viewers"])

        assert admin_retirement.another_admin_exists(store, groups, "admin") is False

    def test_an_org_scoped_admin_does_not_count(self, tmp_path: Path, crud):
        """Org-scoped roles bind inside that org, so its members cannot run the deployment."""
        store, groups = self._stores(tmp_path, crud)
        groups.create("acme-admins", roles=["admin"], scope="org:acme")
        store.create("carol", MINTED_SEED, groups=["acme-admins"])

        assert admin_retirement.another_admin_exists(store, groups, "admin") is False


# ── Deployment hints ─────────────────────────────────────────


class TestDeploymentHints:
    def test_injected_target_wins_over_detection(self):
        assert detect_deploy_kind("kubernetes") == KUBERNETES
        assert detect_deploy_kind("docker") == DOCKER

    def test_unknown_target_falls_back_to_runtime_detection(self):
        assert detect_deploy_kind("unknown") in (DOCKER, KUBERNETES, LOCAL)

    def test_kubernetes_command_names_the_namespace_secret_and_key(self):
        command = credential_fetch_command(
            KUBERNETES, namespace="dfe", secret_name="dfe-engine", secret_key="admin-password"
        )
        assert command == (
            "kubectl -n dfe get secret dfe-engine -o jsonpath='{.data.admin-password}' | base64 -d"
        )

    def test_docker_and_local_commands(self):
        assert credential_fetch_command(DOCKER) == "make creds"
        assert "DFE_AUTH_LOCAL_ADMIN_PASSWORD" in credential_fetch_command(LOCAL)

    def test_rotation_command_is_a_store_operation(self):
        assert "patch secret" in rotation_store_command(
            KUBERNETES, namespace="dfe", secret_name="dfe-engine", secret_key="admin-password"
        )
        assert "make up" in rotation_store_command(DOCKER)
