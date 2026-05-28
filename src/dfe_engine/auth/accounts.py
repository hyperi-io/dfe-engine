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

from datetime import UTC, datetime
from pathlib import Path

import bcrypt
from pydantic import BaseModel, Field

from dfe_engine.yaml_utils import yaml_dump, yaml_load

# Dummy hash used for timing-safe rejection of unknown users.
# Generated once at import time; cost=4 is intentionally low (we just need
# a valid hash to pass to checkpw so it doesn't short-circuit).
_DUMMY_HASH: bytes = bcrypt.hashpw(b"dummy-timing-protection", bcrypt.gensalt(rounds=4))


class Account(BaseModel):
    """A local user account."""

    username: str
    password_hash: str
    enabled: bool = True
    groups: list[str] = Field(default_factory=list)
    external: bool = False
    source_provider: str = ""
    last_login_at: str = ""
    created_at: str = ""
    updated_at: str = ""


class AccountStore:
    """YAML-backed store for local user accounts.

    Each account is a ``{username}.yaml`` file in ``accounts_dir``.
    The username is derived from the filename stem and is never stored
    inside the YAML body.
    """

    def __init__(self, accounts_dir: Path) -> None:
        self._dir = Path(accounts_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(
        self,
        username: str,
        password: str,
        *,
        groups: list[str] | None = None,
    ) -> Account:
        """Create a new account.

        Args:
            username: Unique account name.
            password: Plaintext password — bcrypt-hashed before storage.
            groups: Optional list of group memberships.

        Returns:
            The newly created Account.

        Raises:
            ValueError: If an account with this username already exists.
        """
        path = self._path(username)
        if path.exists():
            raise ValueError(f"Account already exists: {username}")

        now = _now()
        account = Account(
            username=username,
            password_hash=_hash_password(password),
            enabled=True,
            groups=groups or [],
            created_at=now,
            updated_at=now,
        )
        self._write(path, account)
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

    def update(self, username: str, **fields: object) -> Account:
        """Update mutable fields on an existing account.

        Permitted fields: ``enabled``, ``groups``.
        Updating ``username`` or ``password_hash`` directly is not permitted
        (use :meth:`reset_password` to change the password).

        Args:
            username: Account to update.
            **fields: Fields to update (``enabled``, ``groups``).

        Returns:
            The updated Account.

        Raises:
            KeyError: If no account with *username* exists.
        """
        path = self._path(username)
        if not path.exists():
            raise KeyError(username)

        account = self._read(path)

        # Apply permitted field updates only
        if "enabled" in fields:
            account = account.model_copy(update={"enabled": fields["enabled"]})
        if "groups" in fields:
            account = account.model_copy(update={"groups": fields["groups"]})
        if "external" in fields:
            account = account.model_copy(update={"external": fields["external"]})
        if "source_provider" in fields:
            account = account.model_copy(update={"source_provider": fields["source_provider"]})
        if "last_login_at" in fields:
            account = account.model_copy(update={"last_login_at": fields["last_login_at"]})

        account = account.model_copy(update={"updated_at": _now()})
        self._write(path, account)
        return account

    def reset_password(self, username: str, new_password: str) -> None:
        """Replace the stored password hash with a fresh bcrypt hash.

        Args:
            username: Account to update.
            new_password: New plaintext password.

        Raises:
            KeyError: If no account with *username* exists.
        """
        path = self._path(username)
        if not path.exists():
            raise KeyError(username)

        account = self._read(path)
        account = account.model_copy(
            update={
                "password_hash": _hash_password(new_password),
                "updated_at": _now(),
            }
        )
        self._write(path, account)

    def delete(self, username: str) -> None:
        """Remove an account.

        Args:
            username: Account to delete.

        Raises:
            KeyError: If no account with *username* exists.
        """
        path = self._path(username)
        if not path.exists():
            raise KeyError(username)
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


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _hash_password(password: str, rounds: int = 12) -> str:
    """Bcrypt-hash *password* and return the hash as a UTF-8 string."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=rounds)).decode("utf-8")


def _now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()
