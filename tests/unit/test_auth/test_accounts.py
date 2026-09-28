#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_accounts.py
#  Purpose:      Tests for YAML-backed AccountStore
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import bcrypt
import pytest

from dfe_engine.auth import accounts as accounts_module
from dfe_engine.auth.accounts import Account, AccountStore, DocuStoreAccountStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    """AccountStore backed by a temporary directory."""
    return AccountStore(tmp_path / "accounts")


# ---------------------------------------------------------------------------
# Account.create
# ---------------------------------------------------------------------------


class TestCreate:
    def test_create_returns_account(self, store):
        account = store.create("alice", "password123")
        assert isinstance(account, Account)
        assert account.username == "alice"

    def test_create_hashes_password(self, store):
        store.create("alice", "password123")
        account = store.get("alice")
        assert account is not None
        assert account.password_hash.startswith("$2b$")
        assert account.password_hash != "password123"

    def test_create_default_enabled(self, store):
        account = store.create("alice", "password123")
        assert account.enabled is True

    def test_create_empty_groups_by_default(self, store):
        account = store.create("alice", "password123")
        assert account.groups == []

    def test_create_with_groups(self, store):
        account = store.create("alice", "password123", groups=["admins", "ops"])
        assert account.groups == ["admins", "ops"]

    def test_create_with_contact_fields(self, store):
        account = store.create(
            "alice",
            "password123",
            email="alice@example.com",
            phone="+15551212",
            name="Alice Example",
        )
        assert account.email == "alice@example.com"
        assert account.phone == "+15551212"
        assert account.name == "Alice Example"
        got = store.get("alice")
        assert got is not None
        assert got.email == "alice@example.com"
        assert got.phone == "+15551212"
        assert got.name == "Alice Example"

    def test_create_defaults_contact_fields_empty(self, store):
        account = store.create("alice", "password123")
        assert account.email == ""
        assert account.phone == ""
        assert account.name == ""

    def test_create_duplicate_raises(self, store):
        store.create("alice", "password123")
        with pytest.raises(ValueError, match="alice"):
            store.create("alice", "other-password")

    def test_create_rejects_unsafe_name(self, store):
        # Username becomes the {username}.yaml filename stem - path traversal /
        # separators / a trailing newline must be rejected so an account cannot
        # be written outside the store dir or with a malformed name.
        for bad in ("../evil", "a/b", "..", ".hidden", "with space", "bad\n", ""):
            with pytest.raises(ValueError, match="Invalid account name"):
                store.create(bad, "password123")

    def test_create_sets_timestamps(self, store):
        account = store.create("alice", "password123")
        assert account.created_at != ""
        assert account.updated_at != ""

    def test_create_writes_yaml_file(self, store, tmp_path):
        store.create("alice", "password123")
        yaml_file = tmp_path / "accounts" / "alice.yaml"
        assert yaml_file.exists()

    def test_create_username_not_in_yaml(self, store, tmp_path):
        """Username is the filename stem — NOT stored inside the YAML."""
        store.create("alice", "password123")
        yaml_file = tmp_path / "accounts" / "alice.yaml"
        content = yaml_file.read_text()
        assert "username" not in content

    def test_create_creates_dir_if_not_exists(self, tmp_path):
        nested = tmp_path / "deep" / "nested" / "accounts"
        store = AccountStore(nested)
        store.create("bob", "pass")
        assert (nested / "bob.yaml").exists()

    def test_a_refused_create_leaves_the_stored_account_as_it_was(self, store, tmp_path):
        store.create("alice", "password123")
        before = (tmp_path / "accounts" / "alice.yaml").read_bytes()

        with pytest.raises(ValueError, match="Account already exists: alice"):
            store.create("alice", "other-password")

        assert (tmp_path / "accounts" / "alice.yaml").read_bytes() == before
        assert list((tmp_path / "accounts").iterdir()) == [tmp_path / "accounts" / "alice.yaml"]


class TestReplicasCreatingOneAccountAtOnce:
    """Replicas share one accounts directory, and each holds its own store."""

    REPLICAS = 8

    def test_exactly_one_create_succeeds_and_its_account_is_the_one_stored(self, tmp_path):
        replicas = [AccountStore(tmp_path / "accounts") for _ in range(self.REPLICAS)]
        barrier = threading.Barrier(self.REPLICAS)

        def create(index: int) -> str | None:
            password = f"password-{index}"
            barrier.wait(timeout=10)
            try:
                replicas[index].create("alice", password)
            except ValueError:
                return None
            return password

        with ThreadPoolExecutor(max_workers=self.REPLICAS) as pool:
            created = [p for p in pool.map(create, range(self.REPLICAS)) if p is not None]

        assert len(created) == 1, created
        assert replicas[0].verify_password("alice", created[0])


# ---------------------------------------------------------------------------
# AccountStore.get
# ---------------------------------------------------------------------------


class TestGet:
    def test_get_existing_returns_account(self, store):
        store.create("alice", "password123")
        account = store.get("alice")
        assert account is not None
        assert account.username == "alice"

    def test_get_nonexistent_returns_none(self, store):
        result = store.get("nobody")
        assert result is None

    @pytest.mark.parametrize("name", ["../accounts/alice", "./alice"])
    def test_get_by_a_name_no_account_can_have_misses(self, store, name):
        """A session subject reaches this lookup, so it never resolves a path onto an account."""
        store.create("alice", "password123")
        assert store.get(name) is None

    def test_get_username_from_filename_not_yaml(self, store):
        """Username must come from the filename stem, not YAML content."""
        store.create("alice", "password123")
        account = store.get("alice")
        assert account.username == "alice"

    def test_get_preserves_groups(self, store):
        store.create("alice", "password123", groups=["ops", "dev"])
        account = store.get("alice")
        assert account.groups == ["ops", "dev"]

    def test_get_legacy_yaml_without_contact_fields(self, store, tmp_path):
        yaml_file = tmp_path / "accounts" / "legacy.yaml"
        yaml_file.write_text(
            "password_hash: '!'\n"
            "enabled: true\n"
            "groups: []\n"
            "external: false\n"
            "source_provider: ''\n"
            "external_id: ''\n"
            "last_login_at: ''\n"
            "created_at: '2026-01-01T00:00:00+00:00'\n"
            "updated_at: '2026-01-01T00:00:00+00:00'\n"
            "attributes: {}\n"
        )
        account = store.get("legacy")
        assert account is not None
        assert account.email == ""
        assert account.phone == ""
        assert account.name == ""
        assert account.blocked is False
        assert account.disabled_at == ""
        assert account.blocked_at == ""


class TestANameThatIsAPath:
    """Every lookup joins the name onto the store directory, so every one refuses a path."""

    OUTSIDE = "../elsewhere/outside"

    @pytest.fixture
    def outside(self, tmp_path):
        """An account file one directory over, where :attr:`OUTSIDE` resolves from the store."""
        AccountStore(tmp_path / "elsewhere").create("outside", "password123")
        return tmp_path / "elsewhere" / "outside.yaml"

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param(lambda s, n: s.update(n, enabled=False), id="update"),
            pytest.param(lambda s, n: s.reset_password(n, "another-pw"), id="reset_password"),
            pytest.param(lambda s, n: s.set_attributes(n, {"k": "v"}), id="set_attributes"),
            pytest.param(lambda s, n: s.delete(n), id="delete"),
        ],
    )
    def test_a_change_raises_and_leaves_the_file(self, store, outside, change):
        before = outside.read_bytes()

        with pytest.raises(KeyError):
            change(store, self.OUTSIDE)

        assert outside.read_bytes() == before

    def test_a_password_checked_against_it_never_matches(self, store, outside):
        assert store.verify_password(self.OUTSIDE, "password123") is False


class TestANameThatDiffersOnlyInCase:
    """A case-blind filesystem opens bob.yaml for Bob, which would make Bob's session bob's."""

    def test_it_finds_nothing(self, store, tmp_path):
        store.create("bob", "password123", phone="+61 2 5550 0001")
        if not (tmp_path / "accounts" / "BOB.yaml").exists():
            pytest.skip("this filesystem tells names apart by case")

        assert store.get("Bob") is None
        with pytest.raises(KeyError):
            store.update("Bob", phone="+61 2 5550 9999")
        assert store.get("bob").phone == "+61 2 5550 0001"

    def test_the_probe_answers_for_a_directory_whose_name_has_no_letters(self, tmp_path):
        """2024 swapcases to itself, so the directory's own name cannot be the probe."""
        directory = tmp_path / "2024"
        directory.mkdir()
        (directory / "Case.yaml").write_text("", encoding="utf-8")
        folds_case = (directory / "CASE.yaml").exists()
        (directory / "Case.yaml").unlink()

        assert accounts_module._ignores_case(directory) is folds_case
        assert list(directory.iterdir()) == []

    def test_a_store_in_such_a_directory_finds_nothing_under_another_case(self, tmp_path):
        store = AccountStore(tmp_path / "2024")
        store.create("bob", "password123")
        if not (tmp_path / "2024" / "BOB.yaml").exists():
            pytest.skip("this filesystem tells names apart by case")

        assert store.get("Bob") is None


def test_the_stores_annotations_can_be_read():
    """Both stores define list(), which a bare list[...] in their signatures would name."""
    assert AccountStore.list.__annotations__["return"] == list[Account]
    assert AccountStore.create.__annotations__["groups"] == list[str] | None
    assert DocuStoreAccountStore.list.__annotations__["return"] == list[Account]


# ---------------------------------------------------------------------------
# AccountStore.list
# ---------------------------------------------------------------------------


class TestList:
    def test_list_empty_returns_empty(self, store):
        assert store.list() == []

    def test_list_single_account(self, store):
        store.create("alice", "password123")
        accounts = store.list()
        assert len(accounts) == 1
        assert accounts[0].username == "alice"

    def test_list_multiple_accounts(self, store):
        store.create("charlie", "pw3")
        store.create("alice", "pw1")
        store.create("bob", "pw2")
        accounts = store.list()
        assert len(accounts) == 3
        usernames = {a.username for a in accounts}
        assert usernames == {"alice", "bob", "charlie"}

    def test_list_returns_account_objects(self, store):
        store.create("alice", "pw")
        for account in store.list():
            assert isinstance(account, Account)


# ---------------------------------------------------------------------------
# AccountStore.verify_password
# ---------------------------------------------------------------------------


def _cost(digest: bytes | str) -> int:
    """The bcrypt cost a hash was made at (``$2b$<cost>$...``)."""
    text = digest.decode() if isinstance(digest, bytes) else digest
    return int(text.split("$")[2])


@pytest.fixture
def production_bcrypt_cost(monkeypatch):
    """Lift this tree's cost-4 patch, and keep the cost-12 dummy out of every other test."""
    monkeypatch.undo()
    accounts_module._dummy_hash.cache_clear()
    yield
    accounts_module._dummy_hash.cache_clear()


class TestVerifyPassword:
    def test_an_unknown_user_is_checked_at_the_cost_real_hashes_are_made_at(
        self, store, monkeypatch, production_bcrypt_cost
    ):
        """A cheaper check for a name with no account answers faster, naming it free."""
        store.create("alice", "secret123")
        checked: list[bytes] = []
        monkeypatch.setattr(bcrypt, "checkpw", lambda _pw, digest: checked.append(digest) or False)

        store.verify_password("nobody", "secret123")

        assert _cost(checked[0]) == _cost(store.get("alice").password_hash)

    def test_correct_password_returns_true(self, store):
        store.create("alice", "secret123")
        assert store.verify_password("alice", "secret123") is True

    def test_wrong_password_returns_false(self, store):
        store.create("alice", "secret123")
        assert store.verify_password("alice", "wrongpass") is False

    def test_nonexistent_user_returns_false(self, store):
        result = store.verify_password("nobody", "anypass")
        assert result is False

    def test_empty_password_wrong_returns_false(self, store):
        store.create("alice", "secret123")
        assert store.verify_password("alice", "") is False

    def test_verify_after_reset_uses_new_password(self, store):
        store.create("alice", "oldpass")
        store.reset_password("alice", "newpass")
        assert store.verify_password("alice", "newpass") is True
        assert store.verify_password("alice", "oldpass") is False


# ---------------------------------------------------------------------------
# AccountStore.update
# ---------------------------------------------------------------------------


class TestUpdate:
    def test_update_disable_account(self, store):
        store.create("alice", "password123")
        account = store.update("alice", enabled=False)
        assert account.enabled is False
        assert account.disabled_at != ""

    def test_update_enable_account(self, store):
        store.create("alice", "password123")
        store.update("alice", enabled=False)
        account = store.update("alice", enabled=True)
        assert account.enabled is True
        assert account.disabled_at == ""

    def test_reenable_clears_a_stale_disabled_at(self, store):
        store.create("alice", "password123")
        store.update("alice", enabled=False)
        account = store.update(
            "alice",
            enabled=True,
            disabled_at="2026-01-01T00:00:00+00:00",
        )
        assert account.enabled is True
        assert account.disabled_at == ""
        assert store.get("alice").disabled_at == ""

    def test_disable_sets_a_new_timestamp(self, store):
        store.create("alice", "password123")
        first = store.update("alice", enabled=False)
        store.update("alice", enabled=True)
        time.sleep(0.01)
        second = store.update(
            "alice",
            enabled=False,
            disabled_at="2026-01-01T00:00:00+00:00",
        )
        assert second.enabled is False
        assert second.disabled_at != ""
        assert second.disabled_at != "2026-01-01T00:00:00+00:00"
        assert second.disabled_at != first.disabled_at

    def test_update_change_groups(self, store):
        store.create("alice", "password123", groups=["ops"])
        account = store.update("alice", groups=["admins", "dev"])
        assert account.groups == ["admins", "dev"]

    def test_update_contact_fields(self, store):
        store.create("alice", "password123")
        account = store.update(
            "alice",
            email="alice@example.com",
            phone="+1555",
            name="Alice Example",
        )
        assert account.email == "alice@example.com"
        assert account.phone == "+1555"
        assert account.name == "Alice Example"
        got = store.get("alice")
        assert got.email == "alice@example.com"
        assert got.phone == "+1555"
        assert got.name == "Alice Example"

    def test_update_returns_updated_account(self, store):
        store.create("alice", "password123")
        result = store.update("alice", enabled=False)
        assert isinstance(result, Account)
        assert result.enabled is False

    def test_update_persists_to_disk(self, store):
        store.create("alice", "password123")
        store.update("alice", enabled=False, groups=["ops"])
        # Re-read from disk
        account = store.get("alice")
        assert account.enabled is False
        assert account.groups == ["ops"]

    def test_update_nonexistent_raises(self, store):
        with pytest.raises(KeyError, match="nobody"):
            store.update("nobody", enabled=False)

    def test_update_updates_timestamp(self, store):
        store.create("alice", "password123")
        time.sleep(0.01)  # ensure time advances
        updated = store.update("alice", enabled=True)
        # timestamp should be set (not missing)
        assert updated.updated_at != ""

    def test_account_external_fields(self, tmp_path):
        store = AccountStore(tmp_path / "accounts")
        store.create("external-user", "", groups=["viewers"])
        store.update(
            "external-user",
            external=True,
            source_provider="entra",
            last_login_at="2026-04-03T00:00:00Z",
        )
        acct = store.get("external-user")
        assert acct.external is True
        assert acct.source_provider == "entra"
        assert acct.last_login_at == "2026-04-03T00:00:00Z"
        assert acct.blocked is False

    def test_update_blocked_round_trips(self, store):
        store.create("alice", "password123")
        updated = store.update("alice", blocked=True)
        assert updated.blocked is True
        assert updated.blocked_at != ""
        assert store.get("alice").blocked is True
        assert store.get("alice").blocked_at == updated.blocked_at

        cleared = store.update("alice", blocked=False)
        assert cleared.blocked is False
        assert cleared.blocked_at == ""

    def test_unblock_clears_a_stale_blocked_at(self, store):
        store.create("alice", "password123")
        store.update("alice", blocked=True)
        account = store.update(
            "alice",
            blocked=False,
            blocked_at="2026-01-01T00:00:00+00:00",
        )
        assert account.blocked is False
        assert account.blocked_at == ""
        assert store.get("alice").blocked_at == ""

    def test_block_sets_a_new_timestamp(self, store):
        store.create("alice", "password123")
        first = store.update("alice", blocked=True)
        store.update("alice", blocked=False)
        time.sleep(0.01)
        second = store.update(
            "alice",
            blocked=True,
            blocked_at="2026-01-01T00:00:00+00:00",
        )
        assert second.blocked is True
        assert second.blocked_at != ""
        assert second.blocked_at != "2026-01-01T00:00:00+00:00"
        assert second.blocked_at != first.blocked_at


# ---------------------------------------------------------------------------
# AccountStore.reset_password
# ---------------------------------------------------------------------------


class TestResetPassword:
    def test_new_password_works(self, store):
        store.create("alice", "oldpass")
        store.reset_password("alice", "newpass")
        assert store.verify_password("alice", "newpass") is True

    def test_old_password_fails_after_reset(self, store):
        store.create("alice", "oldpass")
        store.reset_password("alice", "newpass")
        assert store.verify_password("alice", "oldpass") is False

    def test_reset_persists_to_disk(self, store):
        store.create("alice", "oldpass")
        store.reset_password("alice", "newpass")
        # Re-read account
        account = store.get("alice")
        assert bcrypt.checkpw(b"newpass", account.password_hash.encode())

    def test_reset_nonexistent_raises(self, store):
        with pytest.raises(KeyError, match="nobody"):
            store.reset_password("nobody", "newpass")


# ---------------------------------------------------------------------------
# AccountStore.delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_removes_account(self, store):
        store.create("alice", "password123")
        store.delete("alice")
        assert store.get("alice") is None

    def test_delete_removes_file(self, store, tmp_path):
        store.create("alice", "password123")
        yaml_file = tmp_path / "accounts" / "alice.yaml"
        assert yaml_file.exists()
        store.delete("alice")
        assert not yaml_file.exists()

    def test_delete_nonexistent_raises(self, store):
        with pytest.raises(KeyError, match="nobody"):
            store.delete("nobody")

    def test_delete_does_not_affect_other_accounts(self, store):
        store.create("alice", "pw1")
        store.create("bob", "pw2")
        store.delete("alice")
        assert store.get("bob") is not None
        assert len(store.list()) == 1
