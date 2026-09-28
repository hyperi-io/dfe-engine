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

import secrets
from pathlib import Path

import pytest
from dulwich import porcelain

from dfe_engine.auth import account_durability, admin_retirement, breakglass
from dfe_engine.auth.accounts import AccountStore
from dfe_engine.auth.bootstrap import (
    DefaultCredentialsError,
    admin_account_password,
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
from dfe_engine.yaml_utils import yaml_load

MINTED_ADMIN = secrets.token_urlsafe(16)
MINTED_BREAKGLASS = secrets.token_urlsafe(16)
MINTED_SEED = secrets.token_urlsafe(16)
# The password an unset config issues to the admin.
SHIPPED_DEFAULT = admin_account_password()


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
    def test_dev_posture_runs_on_the_default(self, env):
        require_admin_password(SHIPPED_DEFAULT, env)
        require_admin_password("", env)

    @pytest.mark.parametrize("env", ["production", "dev"])
    def test_a_minted_password_passes_in_any_posture(self, env):
        require_admin_password(MINTED_ADMIN, env)

    @pytest.mark.parametrize("env", ["production", "dev"])
    def test_a_retired_admin_starts_with_no_password_at_all(self, env):
        """The point of retiring: the operator deletes the injected credential."""
        require_admin_password("", env, retired=True)

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

    def test_the_admin_is_issued_with_a_forced_change(self, tmp_path: Path):
        store, *_ = bootstrap_auth(tmp_path / "auth", default_admin_password=MINTED_ADMIN)

        admin = store.get("admin")
        assert admin.password_change_required is True
        assert store.verify_password("admin", MINTED_ADMIN)
        assert admin.seeded_password_hash == admin.password_hash

    def test_an_issued_password_nobody_changed_still_follows_config(self, tmp_path: Path):
        auth_dir = tmp_path / "auth"
        store, *_ = bootstrap_auth(auth_dir, default_admin_password=MINTED_ADMIN)
        store.reset_password("admin", MINTED_ADMIN + "-issued-again", change_required=True)

        store, *_ = bootstrap_auth(auth_dir, default_admin_password=MINTED_ADMIN)

        assert store.verify_password("admin", MINTED_ADMIN)
        assert store.get("admin").password_change_required is True

    def test_the_owners_change_survives_the_next_boot(self, tmp_path: Path):
        auth_dir = tmp_path / "auth"
        own = MINTED_ADMIN + "-the-owners-own"
        store, *_ = bootstrap_auth(auth_dir, default_admin_password=MINTED_ADMIN)
        store.reset_password("admin", own)

        store, *_ = bootstrap_auth(auth_dir, default_admin_password=MINTED_ADMIN)

        assert store.verify_password("admin", own)
        assert not store.verify_password("admin", MINTED_ADMIN)
        assert store.get("admin").password_change_required is False

    def test_a_rotated_injected_password_is_issued_again(self, tmp_path: Path):
        """A new value in the secret store is a new issue, so the change is due again."""
        auth_dir = tmp_path / "auth"
        rotated = MINTED_ADMIN + "-rotated"
        store, *_ = bootstrap_auth(auth_dir, default_admin_password=MINTED_ADMIN)
        store.reset_password("admin", MINTED_ADMIN + "-the-owners-own")

        store, *_ = bootstrap_auth(auth_dir, default_admin_password=rotated)

        assert store.verify_password("admin", rotated)
        assert store.get("admin").password_change_required is True

    def test_the_owners_password_written_back_into_config_is_kept(self, tmp_path: Path):
        """A deployment that records the owner's new password as the injected one has not rotated it."""
        auth_dir = tmp_path / "auth"
        own = MINTED_ADMIN + "-the-owners-own"
        store, *_ = bootstrap_auth(auth_dir, default_admin_password=MINTED_ADMIN)
        store.reset_password("admin", own)
        owners_hash = store.get("admin").password_hash

        store, *_ = bootstrap_auth(auth_dir, default_admin_password=own)

        admin = store.get("admin")
        assert admin.password_change_required is False
        assert admin.password_hash == owners_hash
        assert store.verify_password("admin", own)

    def test_recording_the_owners_password_does_not_reissue_it_on_recreate(
        self, tmp_path: Path, crud
    ):
        """Each recreate rebuilds the store from the deploy repo and boots on the recorded password."""
        own = MINTED_ADMIN + "-the-owners-own"
        store, *_ = bootstrap_auth(
            tmp_path / "auth", default_admin_password=MINTED_ADMIN, gitcrud=crud
        )
        store.reset_password("admin", own)
        owners_hash = store.get("admin").password_hash
        account_durability.publish_direct(crud, store.get("admin"), summary="reset password")

        for recreate in range(3):
            store, *_ = bootstrap_auth(
                tmp_path / f"recreated-{recreate}", default_admin_password=own, gitcrud=crud
            )
            assert store.get("admin").password_change_required is False
            assert store.get("admin").password_hash == owners_hash

        assert crud.get("accounts", "admin")["password_hash"] == owners_hash

    def test_an_admin_from_before_the_flag_is_issued_a_forced_change(self, tmp_path: Path):
        """An upgraded deployment's admin has no flag and no digest, yet serves the minted value."""
        auth_dir = tmp_path / "auth"
        AccountStore(auth_dir / "accounts").create("admin", MINTED_ADMIN, groups=["dfe-admins"])

        store, *_ = bootstrap_auth(auth_dir, default_admin_password=MINTED_ADMIN)

        assert store.verify_password("admin", MINTED_ADMIN)
        assert store.get("admin").password_change_required is True

    def test_the_owners_change_survives_a_rebuilt_store(self, tmp_path: Path, crud):
        """The deploy repo carries the owner's hash and the cleared flag through a rebuild."""
        own = MINTED_ADMIN + "-the-owners-own"
        store, *_ = bootstrap_auth(
            tmp_path / "auth", default_admin_password=MINTED_ADMIN, gitcrud=crud
        )
        store.reset_password("admin", own)
        account_durability.publish_direct(crud, store.get("admin"), summary="reset password")

        store, *_ = bootstrap_auth(
            tmp_path / "rebuilt-auth", default_admin_password=MINTED_ADMIN, gitcrud=crud
        )

        assert store.verify_password("admin", own)
        assert store.get("admin").password_change_required is False

    def test_named_seeds_are_not_issued_a_forced_change(self, tmp_path: Path):
        seeds = [SeedAccount(username="kay", password=MINTED_SEED, groups=["dfe-viewers"])]

        store, *_ = bootstrap_auth(
            tmp_path / "auth", default_admin_password=MINTED_ADMIN, seed_accounts=seeds
        )

        assert store.get("kay").password_change_required is False

    def test_the_hash_is_stable_across_boots_after_a_rotation(self, tmp_path: Path, crud):
        """The reconciled hash reaches the deploy repo, so hydration stops re-minting.

        Unpublished, the deploy repo keeps the pre-rotation hash; hydration puts it
        back on the next boot and the reconcile resets it again, minting a fresh
        bcrypt hash every boot and leaving the durable copy permanently superseded.
        """
        auth_dir = tmp_path / "auth"
        rotated = MINTED_ADMIN + "-rotated"
        bootstrap_auth(auth_dir, default_admin_password=MINTED_ADMIN, gitcrud=crud)

        hashes = set()
        for _ in range(3):
            store, *_ = bootstrap_auth(auth_dir, default_admin_password=rotated, gitcrud=crud)
            hashes.add(store.get("admin").password_hash)

        assert len(hashes) == 1, "the admin hash is re-minted on every boot"
        assert crud.get("accounts", "admin")["password_hash"] == hashes.pop()
        assert store.verify_password("admin", rotated)

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
        assert store.get(breakglass.USERNAME).email == "breakglass@dfe.local"
        # Break-glass keeps its own first-login rules: it is never issued a forced change.
        assert store.get(breakglass.USERNAME).password_change_required is False

    def test_break_glass_email_is_backfilled_when_missing(self, tmp_path: Path, crud):
        store, *_ = bootstrap_auth(
            tmp_path / "auth",
            default_admin_password=MINTED_ADMIN,
            gitcrud=crud,
            breakglass_password=MINTED_BREAKGLASS,
        )
        store.update(breakglass.USERNAME, email="")

        store, *_ = bootstrap_auth(
            tmp_path / "auth",
            default_admin_password=MINTED_ADMIN,
            gitcrud=crud,
        )
        assert store.get(breakglass.USERNAME).email == "breakglass@dfe.local"

    def test_recovery_email_seeds_admin_and_breakglass(self, tmp_path: Path, crud):
        store, *_ = bootstrap_auth(
            tmp_path / "auth",
            default_admin_password=MINTED_ADMIN,
            gitcrud=crud,
            breakglass_password=MINTED_BREAKGLASS,
            recovery_email="ops@example.com",
        )

        assert store.get("admin").email == "ops@example.com"
        assert store.get(breakglass.USERNAME).email == "ops@example.com"

    def test_recovery_email_reconciles_breakglass_over_fallback(self, tmp_path: Path, crud):
        auth_dir = tmp_path / "auth"
        store, *_ = bootstrap_auth(
            auth_dir,
            default_admin_password=MINTED_ADMIN,
            gitcrud=crud,
            breakglass_password=MINTED_BREAKGLASS,
        )
        assert store.get(breakglass.USERNAME).email == "breakglass@dfe.local"

        store, *_ = bootstrap_auth(
            auth_dir,
            default_admin_password=MINTED_ADMIN,
            gitcrud=crud,
            recovery_email="ops@example.com",
        )
        assert store.get("admin").email == "ops@example.com"
        assert store.get(breakglass.USERNAME).email == "ops@example.com"

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


# ── Two replicas, one deploy repo ────────────────────────────


def _shared_deploy_repo(tmp_path: Path) -> tuple[str, str]:
    """A bare deploy repo with one commit, and the branch it lives on."""
    remote = tmp_path / "remote.git"
    porcelain.init(str(remote), bare=True)
    seed = tmp_path / "seed"
    porcelain.clone(str(remote), str(seed))
    (seed / "README").write_text("seed\n", encoding="utf-8")
    porcelain.add(str(seed), paths=[str(seed / "README")])
    porcelain.commit(str(seed), message=b"init", author=b"t <t@t>", committer=b"t <t@t>")
    branch = porcelain.active_branch(str(seed)).decode()
    porcelain.push(str(seed), str(remote), f"refs/heads/{branch}".encode())
    return str(remote), branch


def _replica(tmp_path: Path, name: str, remote: str, branch: str) -> GitCrud:
    """A replica's own clone of the shared deploy repo."""
    repo = GitopsRepo(local_path=str(tmp_path / name), repo_url=remote, branch=branch, push=True)
    return GitCrud(repo, default_registry())


class TestTwoReplicas:
    """Two engine replicas boot together against one deploy repo (dfe-engine#346)."""

    def test_the_second_replica_adopts_the_committed_hash(self, tmp_path: Path):
        remote, branch = _shared_deploy_repo(tmp_path)
        first = _replica(tmp_path, "a", remote, branch)
        second = _replica(tmp_path, "b", remote, branch)

        minted = breakglass.mint_hash(first, MINTED_BREAKGLASS)
        # The second replica's clone predates the mint, so only the remote has it.
        adopted = breakglass.mint_hash(second, MINTED_BREAKGLASS)

        assert adopted == minted

    def test_one_break_glass_hash_reaches_the_deploy_repo(self, tmp_path: Path):
        remote, branch = _shared_deploy_repo(tmp_path)
        first = _replica(tmp_path, "a", remote, branch)
        second = _replica(tmp_path, "b", remote, branch)

        minted = breakglass.mint_hash(first, MINTED_BREAKGLASS)
        breakglass.mint_hash(second, MINTED_BREAKGLASS)

        check = tmp_path / "check"
        porcelain.clone(remote, str(check))
        doc = yaml_load(check / "governance" / "settings" / "auth.yaml")
        assert doc["breakglass"]["password_hash"] == minted


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
        groups.add_member("dfe-admins", "alice")

        assert admin_retirement.another_admin_exists(store, groups, "admin") is True

    def test_an_admin_group_named_only_in_the_account_record_does_not_count(
        self, tmp_path: Path, crud
    ):
        """The group file is the authority: ops's own list names dfe-admins, the file does not."""
        store, groups = self._stores(tmp_path, crud)
        store.create("ops", MINTED_SEED, groups=["dfe-admins"])

        assert admin_retirement.another_admin_exists(store, groups, "admin") is False

    def test_an_idp_account_asserting_the_admin_group_counts(self, tmp_path: Path, crud):
        """An IdP-owned account holds what its IdP asserts, as a session does."""
        store, groups = self._stores(tmp_path, crud)
        store.create("jane-corp-com", "", groups=["dfe-admins"])
        store.update("jane-corp-com", external=True, source_provider="entra")

        assert admin_retirement.another_admin_exists(store, groups, "admin") is True

    @pytest.mark.parametrize(
        "state",
        [
            pytest.param({"enabled": False}, id="disabled"),
            pytest.param({"blocked": True}, id="blocked"),
        ],
    )
    def test_an_account_that_cannot_hold_a_session_does_not_count(
        self, tmp_path: Path, crud, state
    ):
        store, groups = self._stores(tmp_path, crud)
        store.create("alice", MINTED_SEED, groups=["dfe-admins"])
        groups.add_member("dfe-admins", "alice")
        store.update("alice", **state)

        assert admin_retirement.another_admin_exists(store, groups, "admin") is False

    def test_an_account_on_an_issued_password_does_not_count(self, tmp_path: Path, crud):
        """Until the change its session holds no roles, so it cannot run the deployment."""
        store, groups = self._stores(tmp_path, crud)
        store.create("alice", MINTED_SEED, groups=["dfe-admins"], change_required=True)
        groups.add_member("dfe-admins", "alice")

        assert admin_retirement.another_admin_exists(store, groups, "admin") is False

    def test_a_user_without_the_admin_role_does_not_count(self, tmp_path: Path, crud):
        store, groups = self._stores(tmp_path, crud)
        store.create("bob", MINTED_SEED, groups=["dfe-viewers"])

        assert admin_retirement.another_admin_exists(store, groups, "admin") is False

    def test_an_org_scoped_admin_does_not_count(self, tmp_path: Path, crud):
        """Org-scoped roles bind inside that org, so its members cannot run the deployment."""
        store, groups = self._stores(tmp_path, crud)
        groups.create("acme-admins", roles=["admin"], members=["carol"], scope="org:acme")
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
