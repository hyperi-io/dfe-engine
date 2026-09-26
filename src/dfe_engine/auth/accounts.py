#  Project:      dfe-engine
#  File:         auth/accounts.py
#  Purpose:      YAML-backed CRUD store for local user accounts with bcrypt passwords
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""YAML-backed account store for local user management.

Accounts are stored as individual YAML files: ``{accounts_dir}/{username}.yaml``.
The username is the filename stem — it is NOT written into the YAML body.
Passwords are bcrypt-hashed (cost=12) before storage.

Usage::

    from pathlib import Path
    from dfe_engine.auth.accounts import Account, AccountStore

    store = AccountStore(Path("/etc/dfe/accounts"))
    store.create("alice", "s3cr3t", groups=["admins"])
    assert store.verify_password("alice", "s3cr3t")
    store.update("alice", enabled=False)
    store.delete("alice")
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import bcrypt
from pydantic import BaseModel, Field

from dfe_engine.auth.protected_accounts import resolve_floor
from dfe_engine.yaml_utils import yaml_dump, yaml_load

if TYPE_CHECKING:
    from dfe_engine.store.documents import DocuStore

# Dummy hash used for timing-safe rejection of unknown users.
# Generated once at import time; cost=4 is intentionally low (we just need
# a valid hash to pass to checkpw so it doesn't short-circuit).
_DUMMY_HASH: bytes = bcrypt.hashpw(b"dummy-timing-protection", bcrypt.gensalt(rounds=4))

# Account name becomes the filename stem ({name}.yaml), so it must be a safe
# stem - reject path traversal / separators. \Z (not $) anchors the true end of
# string so a trailing newline cannot slip into the filename.
_VALID_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}\Z")

# Stored for accounts with NO usable local password (external / IdP-owned /
# JIT-provisioned). Not a valid bcrypt hash ($2...), so verify_password never
# matches - an external identity can only authenticate via its IdP, never local login.
_UNUSABLE_PASSWORD_HASH = "!"


class Account(BaseModel):
    """A local user account."""

    username: str
    password_hash: str
    enabled: bool = True
    blocked: bool = False
    groups: list[str] = Field(default_factory=list)
    external: bool = False
    source_provider: str = ""
    external_id: str = ""
    """Provider-specific external identifier (SCIM externalId / IdP object ID)."""
    last_login_at: str = ""
    disabled_at: str = ""
    blocked_at: str = ""
    email: str = ""
    phone: str = ""
    name: str = ""
    created_at: str = ""
    updated_at: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    """Free-form NON-sensitive attributes (nested JSON, KVP is the floor).

    Stored inline on the account and round-trips through both backends. Never put
    secrets here - a broad account read returns this blob; sensitive attributes
    live in the separate keyed store (:mod:`dfe_engine.auth.attributes`)."""
    password_change_required: bool = False
    """The password was issued to the account rather than chosen by its owner.

    While set, the API refuses the account everything but changing its own
    password (:mod:`dfe_engine.api.password_change`); the owner's change clears it."""
    seeded_password_hash: str = ""
    """Digest of the last password issued with ``password_change_required``.

    Kept after the owner's change, so the boot reconcile can tell an injected
    password it already issued from one the deployment has since rotated."""

    def session_denied(self) -> tuple[str, str] | None:
        """Return ``(code, message)`` when this account may not hold a session."""
        if self.blocked:
            return "account_blocked", "Account blocked"
        if not self.enabled:
            return "unauthorized", "Account disabled"
        return None


class AccountStore:
    """YAML-backed store for local user accounts.

    Each account is a ``{username}.yaml`` file in ``accounts_dir``.
    The username is derived from the filename stem and is never stored
    inside the YAML body.

    Attributes:
        protected: The recovery credentials this store refuses to lock out
            (:mod:`dfe_engine.auth.protected_accounts`). Public so a caller can
            refuse a batch before applying any of it.
    """

    def __init__(self, accounts_dir: Path, *, admin_name: str = "") -> None:
        self._dir = Path(accounts_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self.protected = resolve_floor(admin_name)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(
        self,
        username: str,
        password: str,
        *,
        groups: list[str] | None = None,
        email: str = "",
        phone: str = "",
        name: str = "",
        change_required: bool = False,
    ) -> Account:
        """Create a new account.

        Args:
            username: Unique account name.
            password: Plaintext password — bcrypt-hashed before storage.
            groups: Optional list of group memberships.
            email: Optional contact email.
            phone: Optional contact phone.
            name: Optional display name (distinct from ``username``).
            change_required: The password is issued, not the owner's own, so the
                owner must replace it before the API serves them.

        Returns:
            The newly created Account.

        Raises:
            ValueError: If an account with this username already exists.
        """
        if not _VALID_NAME.match(username):
            raise ValueError(f"Invalid account name: {username!r}")
        path = self._path(username)
        if path.exists():
            raise ValueError(f"Account already exists: {username}")

        account = _new_account(
            username,
            password,
            groups=groups,
            email=email,
            phone=phone,
            name=name,
            change_required=change_required,
        )
        self._write(path, account)
        return account

    def put(self, account: Account, *, allow_protected: bool = False) -> Account:
        """Upsert a complete account (used to restore from the durable deploy repo).

        Unlike :meth:`create` / :meth:`reset_password`, this writes the account
        verbatim -- hash and timestamps included -- so a hydration pass can put back
        exactly what the deploy repo holds. Raises on an invalid username.

        Args:
            account: The record to store verbatim.
            allow_protected: Write a recovery credential that the floor would
                otherwise refuse -- a disabled admin restored from the deploy
                repo. Reserved for the reconcile paths.

        Returns:
            The account as written.

        Raises:
            ValueError: The username is not a safe filename stem.
            ProtectedAccountError: The record would leave a recovery credential
                unable to log in.
        """
        if not _VALID_NAME.match(account.username):
            raise ValueError(f"Invalid account name: {account.username!r}")
        if not allow_protected:
            self.protected.check_account_state(
                account.username,
                enabled=account.enabled,
                blocked=account.blocked,
                groups=account.groups,
            )
        self._write(self._path(account.username), account)
        return account

    def get(self, username: str) -> Account | None:
        """Return the account for *username*, or None if not found.

        Args:
            username: Account name to look up.

        Returns:
            Account if found, None otherwise.
        """
        path = self._path(username)
        if not path.exists():
            return None
        return self._read(path)

    def list(self) -> list[Account]:
        """Return all accounts in the store.

        Returns:
            List of Account objects (order is filesystem-dependent).
        """
        return [self._read(p) for p in sorted(self._dir.glob("*.yaml"))]

    def update(self, username: str, *, allow_protected: bool = False, **fields: object) -> Account:
        """Update mutable fields on an existing account.

        Permitted fields: ``enabled``, ``blocked``, ``disabled_at``,
        ``blocked_at``, ``groups``, ``email``, ``phone``, ``name``, plus the
        external-identity stamps. Updating ``username`` or ``password_hash``
        directly is not permitted (use :meth:`reset_password` to change the
        password). Re-enabling or unblocking always clears the matching
        ``*_at`` field. Disabling or blocking always writes a new stamp.

        Args:
            username: Account to update.
            allow_protected: Disable or de-role a recovery credential -- admin
                retirement, and nothing else.
            **fields: Fields to update.

        Returns:
            The updated Account.

        Raises:
            KeyError: If no account with *username* exists.
            ProtectedAccountError: The update would disable a recovery
                credential or drop it from the admin-role group.
        """
        path = self._path(username)
        if not path.exists():
            raise KeyError(username)

        account = self._read(path)
        if not allow_protected:
            self.protected.check_account_update(username, fields, account.groups)
        updates = _apply_access_stamps({k: fields[k] for k in _UPDATABLE_FIELDS if k in fields})
        account = account.model_copy(update={**updates, "updated_at": _now()})
        self._write(path, account)
        return account

    def reset_password(
        self, username: str, new_password: str, *, change_required: bool = False
    ) -> None:
        """Replace the stored password hash with a fresh bcrypt hash.

        Args:
            username: Account to update.
            new_password: New plaintext password.
            change_required: The password is issued, not the owner's own. False
                clears a pending change, which is what the owner's own reset does.

        Raises:
            KeyError: If no account with *username* exists.
        """
        path = self._path(username)
        if not path.exists():
            raise KeyError(username)

        account = self._read(path)
        account = account.model_copy(update=_password_update(new_password, change_required))
        self._write(path, account)

    def set_attributes(self, username: str, attributes: dict) -> Account:
        """Full-replace the non-sensitive ``attributes`` blob on an account.

        Args:
            username: Account to update.
            attributes: The new attributes dict (replaces the field wholesale).

        Returns:
            The updated Account.

        Raises:
            KeyError: If no account with *username* exists.
        """
        path = self._path(username)
        if not path.exists():
            raise KeyError(username)

        account = self._read(path)
        account = account.model_copy(update={"attributes": attributes, "updated_at": _now()})
        self._write(path, account)
        return account

    def delete(self, username: str, *, allow_protected: bool = False) -> None:
        """Remove an account.

        Args:
            username: Account to delete.
            allow_protected: Delete a recovery credential -- the e2e-server
                store wipe, and nothing else.

        Raises:
            KeyError: If no account with *username* exists.
            ProtectedAccountError: *username* is a recovery credential.
        """
        path = self._path(username)
        if not path.exists():
            raise KeyError(username)
        if not allow_protected:
            self.protected.check_account_delete(username)
        path.unlink()

    def verify_password(self, username: str, password: str) -> bool:
        """Check whether *password* matches the stored hash.

        Always performs a bcrypt check — even for unknown users — to
        prevent timing-based username enumeration.

        Args:
            username: Account name.
            password: Plaintext password to verify.

        Returns:
            True if the password matches, False otherwise.
        """
        path = self._path(username)
        if not path.exists():
            # Timing-safe rejection: still run bcrypt to prevent oracle attacks
            bcrypt.checkpw(password.encode("utf-8"), _DUMMY_HASH)
            return False

        account = self._read(path)
        # An empty password never authenticates, and an account carrying the
        # unusable-password sentinel (external / IdP-owned / JIT) has no valid
        # bcrypt hash - reject both, timing-safely. Prevents an empty or
        # placeholder password from ever matching a stored account.
        if not password or not account.password_hash.startswith("$2"):
            bcrypt.checkpw(password.encode("utf-8"), _DUMMY_HASH)
            return False
        return bcrypt.checkpw(
            password.encode("utf-8"),
            account.password_hash.encode("utf-8"),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _path(self, username: str) -> Path:
        return self._dir / f"{username}.yaml"

    def _read(self, path: Path) -> Account:
        """Load an Account from a YAML file.

        The username is derived from the filename stem — it is not stored
        inside the YAML body.
        """
        data: dict = yaml_load(path)
        data["username"] = path.stem
        return Account.model_validate(data)

    def _write(self, path: Path, account: Account) -> None:
        """Persist an Account to YAML, omitting the username field."""
        data = account.model_dump(exclude={"username"})
        yaml_dump(data, path)


# Fields update() may change; username and password_hash are excluded (the
# password changes only via reset_password). Shared by both store backends.
_UPDATABLE_FIELDS = (
    "enabled",
    "blocked",
    "disabled_at",
    "blocked_at",
    "groups",
    "external",
    "source_provider",
    "external_id",
    "last_login_at",
    "email",
    "phone",
    "name",
)


class DocuStoreAccountStore:
    """Document-store-backed account store - the same interface as :class:`AccountStore`.

    Persists one :class:`Account` document per username (keyed and unique-indexed
    on ``username``) instead of one YAML file. Every domain rule - name
    validation, bcrypt hashing, the unusable-password sentinel, the protected-name
    floor, and the timing-safe :meth:`verify_password` - is identical to the YAML
    store, so the two are drop-in interchangeable behind the same construction seam.

    Attributes:
        protected: The recovery credentials this store refuses to lock out.
    """

    def __init__(
        self,
        store: DocuStore,
        *,
        collection: str = "accounts",
        admin_name: str = "",
    ) -> None:
        self._c = store.typed(collection, Account, key="username")
        self.protected = resolve_floor(admin_name)

    def create(
        self,
        username: str,
        password: str,
        *,
        groups: list[str] | None = None,
        email: str = "",
        phone: str = "",
        name: str = "",
        change_required: bool = False,
    ) -> Account:
        """Create a new account. Raises ValueError if the name is invalid or taken."""
        if not _VALID_NAME.match(username):
            raise ValueError(f"Invalid account name: {username!r}")
        if self._c.exists(username):
            raise ValueError(f"Account already exists: {username}")
        account = _new_account(
            username,
            password,
            groups=groups,
            email=email,
            phone=phone,
            name=name,
            change_required=change_required,
        )
        self._c.put(username, account)
        return account

    def put(self, account: Account, *, allow_protected: bool = False) -> Account:
        """Upsert a complete account verbatim (restore from the durable deploy repo).

        ``allow_protected`` writes a recovery credential the floor would refuse;
        reserved for the reconcile paths. Raises ProtectedAccountError otherwise.
        """
        if not _VALID_NAME.match(account.username):
            raise ValueError(f"Invalid account name: {account.username!r}")
        if not allow_protected:
            self.protected.check_account_state(
                account.username,
                enabled=account.enabled,
                blocked=account.blocked,
                groups=account.groups,
            )
        self._c.put(account.username, account)
        return account

    def get(self, username: str) -> Account | None:
        """Return the account for *username*, or None if not found."""
        return self._c.get(username)

    def list(self) -> list[Account]:
        """Return all accounts, sorted by username."""
        return self._c.list()

    def update(self, username: str, *, allow_protected: bool = False, **fields: object) -> Account:
        """Update permitted fields on an account. Raises KeyError if missing.

        ``allow_protected`` disables or de-roles a recovery credential -- admin
        retirement only. Raises ProtectedAccountError otherwise.
        """
        account = self._c.get(username)
        if account is None:
            raise KeyError(username)
        if not allow_protected:
            self.protected.check_account_update(username, fields, account.groups)
        updates = _apply_access_stamps({k: fields[k] for k in _UPDATABLE_FIELDS if k in fields})
        account = account.model_copy(update={**updates, "updated_at": _now()})
        self._c.put(username, account)
        return account

    def reset_password(
        self, username: str, new_password: str, *, change_required: bool = False
    ) -> None:
        """Replace the stored hash with a fresh bcrypt hash. Raises KeyError if missing.

        ``change_required`` marks the password as issued; False clears a pending change.
        """
        account = self._c.get(username)
        if account is None:
            raise KeyError(username)
        account = account.model_copy(update=_password_update(new_password, change_required))
        self._c.put(username, account)

    def set_attributes(self, username: str, attributes: dict) -> Account:
        """Full-replace the non-sensitive ``attributes`` blob. Raises KeyError if missing."""
        account = self._c.get(username)
        if account is None:
            raise KeyError(username)
        account = account.model_copy(update={"attributes": attributes, "updated_at": _now()})
        self._c.put(username, account)
        return account

    def delete(self, username: str, *, allow_protected: bool = False) -> None:
        """Remove an account. Raises KeyError if it does not exist.

        ``allow_protected`` deletes a recovery credential -- the e2e-server store
        wipe only. Raises ProtectedAccountError otherwise.
        """
        # Existence first, so a missing name answers KeyError on both backends.
        if self._c.get(username) is None:
            raise KeyError(username)
        if not allow_protected:
            self.protected.check_account_delete(username)
        self._c.delete(username)

    def verify_password(self, username: str, password: str) -> bool:
        """Timing-safe password check - identical semantics to the YAML store."""
        account = self._c.get(username)
        if account is None:
            bcrypt.checkpw(password.encode("utf-8"), _DUMMY_HASH)
            return False
        if not password or not account.password_hash.startswith("$2"):
            bcrypt.checkpw(password.encode("utf-8"), _DUMMY_HASH)
            return False
        return bcrypt.checkpw(
            password.encode("utf-8"),
            account.password_hash.encode("utf-8"),
        )


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _apply_access_stamps(updates: dict[str, object]) -> dict[str, object]:
    """Stamp or clear ``disabled_at`` / ``blocked_at`` when those flags change.

    Disabling or blocking always writes a new stamp. Re-enabling or unblocking
    always resets the matching stamp to empty.
    """
    stamped = dict(updates)
    if "enabled" in stamped:
        stamped["disabled_at"] = "" if stamped["enabled"] else _now()
    if "blocked" in stamped:
        stamped["blocked_at"] = _now() if stamped["blocked"] else ""
    return stamped


def _new_account(
    username: str,
    password: str,
    *,
    groups: list[str] | None,
    email: str,
    phone: str,
    name: str,
    change_required: bool,
) -> Account:
    """The record both stores write for a new account."""
    now = _now()
    digest = hash_password(password) if password else _UNUSABLE_PASSWORD_HASH
    return Account(
        username=username,
        password_hash=digest,
        enabled=True,
        groups=groups or [],
        email=email,
        phone=phone,
        name=name,
        created_at=now,
        updated_at=now,
        password_change_required=change_required,
        seeded_password_hash=digest if change_required else "",
    )


def _password_update(new_password: str, change_required: bool) -> dict[str, object]:
    """The fields a password reset writes; an issued password is also the seeded digest."""
    digest = hash_password(new_password)
    update: dict[str, object] = {
        "password_hash": digest,
        "password_change_required": change_required,
        "updated_at": _now(),
    }
    if change_required:
        update["seeded_password_hash"] = digest
    return update


def hash_password(password: str, rounds: int = 12) -> str:
    """Bcrypt-hash *password* and return the hash as a UTF-8 string."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=rounds)).decode("utf-8")


def matches_digest(password: str, digest: str) -> bool:
    """Whether *digest* was made from *password*; an empty or non-bcrypt digest never matches."""
    if not password or not digest.startswith("$2"):
        return False
    return bcrypt.checkpw(password.encode("utf-8"), digest.encode("utf-8"))


def _now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()
