#  Project:      dfe-engine
#  File:         auth/accounts.py
#  Purpose:      YAML-backed CRUD store for local user accounts with bcrypt passwords
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""YAML-backed account store for local user management.

Accounts are stored as individual YAML files: ``{accounts_dir}/{username}.yaml``.
The username is the filename stem -- it is NOT written into the YAML body.
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

import builtins
import hashlib
import os
import secrets
import tempfile
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

import bcrypt
from pydantic import BaseModel, Field
from scalo.logger import logger

from dfe_engine.auth.protected_accounts import resolve_floor
from dfe_engine.auth.store_names import case_twin, store_key
from dfe_engine.names import VALID_NAME, InvalidNameError
from dfe_engine.yaml_utils import yaml_dump, yaml_dump_string, yaml_load_cached

BCRYPT_ROUNDS = 12
"""The bcrypt cost every stored password hash is made at."""

BCRYPT_MAX_PASSWORD_BYTES = 72
"""The longest password, in UTF-8 bytes, bcrypt hashes. It refuses a longer one."""


@cache
def _dummy_hash() -> bytes:
    """The hash an unknown or passwordless account is checked against.

    Made at the stored hashes' own cost, so a login for an account that does not
    exist takes as long as one for an account that does. Built when the first
    :class:`AccountStore` is made rather than at import, because a hash at that cost
    takes a noticeable fraction of a second, and not on first login, where the extra
    hash would time the first unknown name apart from a real one.
    """
    return bcrypt.hashpw(b"dummy-timing-protection", bcrypt.gensalt(rounds=BCRYPT_ROUNDS))


def _ignores_case(directory: Path) -> bool:
    """Whether *directory* sits on a filesystem that takes ``Bob`` and ``bob`` as one name.

    Probed with a file of its own inside *directory*, since the directory's own name
    may have no letters to flip. Where no probe can be made the answer is True, so
    every lookup checks its entry's name, which is right on either kind.
    """
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix="case-probe-", suffix=".tmp") as f:
            probe = Path(f.name)
            return probe.with_name(probe.name.swapcase()).exists()
    except OSError:
        return True


# Stored for accounts with NO usable local password (external / IdP-owned /
# JIT-provisioned). Not a valid bcrypt hash ($2...), so verify_password never
# matches - an external identity can only authenticate via its IdP, never local login.
_UNUSABLE_PASSWORD_HASH = "!"  # noqa: S105, RUF100 - a marker no bcrypt hash can equal


class AccountExistsError(ValueError):
    """An account is already stored under the name, including one created a moment ago."""


class LinkedIdentity(BaseModel):
    """An IdP sign-in linked to an account, with what its IdP last asserted."""

    key: str
    """The link record's account name: the sign-in's JIT stem."""
    provider: str
    """The OIDC provider the sign-in comes through."""
    subject: str
    """The IdP ``sub`` claim."""
    email: str = ""
    groups: list[str] = Field(default_factory=list)
    """The identifiers the IdP last asserted, which link groups under ``provider``."""
    linked_at: str = ""
    last_login_at: str = ""


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
    subject: str = ""
    """The IdP subject a JIT login created or adopted this account for.

    Several subjects share one JIT stem (``Alice.Smith@corp`` and
    ``alice-smith@corp`` both sanitise to ``alice-smith-corp``), so this is what
    tells the account's own identity from another that sanitises onto it. Empty on
    an account no IdP login has reached yet."""
    oidc_id: str = ""
    """OIDC provider name last used to log this account in.

    Written on every successful OIDC login (JIT create and subsequent). Distinct
    from ``source_provider``, which may stay a SCIM stamp when the same IdP owns
    the account through a binding. Empty until an OIDC login has reached it."""
    last_login_at: str = ""
    disabled_at: str = ""
    blocked_at: str = ""
    email: str = ""
    phone: str = ""
    name: str = ""
    org: str = ""
    """The registered org the account belongs to, which its sessions carry as ``org_id``.

    Empty binds it to no org, as the bootstrap admin and break-glass accounts are,
    and as every account written before the field is: such a session reads no
    org-bound view. A binding grants nothing itself, because a view also needs the
    org among those the account's groups grant or a platform role."""
    created_at: str = ""
    updated_at: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    """Free-form NON-sensitive attributes (nested JSON, KVP is the floor).

    Stored inline on the account and round-trips through the store. Never put
    secrets here - a broad account read returns this blob. Sensitive attributes
    live in the separate keyed store (:mod:`dfe_engine.auth.attributes`)."""
    password_change_required: bool = False
    """The password was issued to the account rather than chosen by its owner.

    While set, the API refuses the account everything but changing its own
    password (:mod:`dfe_engine.api.password_change`). The owner's change clears it."""
    seeded_password_hash: str = ""
    """Digest of the last password issued with ``password_change_required``.

    Kept after the owner's change, so the boot reconcile can tell an injected
    password it already issued from one the deployment has since rotated."""
    session_epoch: str = ""
    """Replaced whenever every session of the account ends: its owner logs out, its
    password changes, or it is disabled, blocked, re-enabled or unblocked. Empty until
    that first happens."""
    session_generation: int = 0
    """How many times every session of the account has ended.

    The epoch is random, so this is what orders two copies of one account: the copy
    with the higher count holds the later epoch, and a restore keeps it
    (:func:`later_session_end`)."""
    previous_username: str = ""
    """Account id before soft-delete anonymization.

    Written when the account is renamed to ``{uuid}-deleted``. Empty on an account
    that has never been soft-deleted."""
    email_verified: bool | None = None
    """Whether ``email`` is vouched for, so a first IdP sign-in may link here by it.

    True: an admin set it, or an IdP trusted for its domain asserted it verified. False: the
    account owner set it, or the IdP did not vouch. None: written before the field existed."""
    linked_to: str = ""
    """Set on a link record: the account its IdP sign-in belongs to. A link record holds no groups and is never listed."""
    identities: list[LinkedIdentity] = Field(default_factory=list)
    """The IdP sign-ins linked to this account."""

    @property
    def is_link_record(self) -> bool:
        """Whether this record only points an IdP sign-in at another account."""
        return bool(self.linked_to)

    @property
    def is_soft_deleted(self) -> bool:
        """Whether this account was anonymized by soft-delete and should read as gone."""
        return bool(self.previous_username) or self.username.endswith("-deleted")

    @property
    def session_subject(self) -> str:
        """The ``sub`` a session token for this account carries, which the per-request lookup binds back to it."""
        return self.subject or self.username

    def session_marker(self) -> str:
        """The value a session token minted for this account now carries.

        It moves with the session epoch, the password and the creation time, so a
        token outlives none of a logout, a password change, or the account being
        deleted and created again under its name.
        """
        material = "\n".join((self.session_epoch, self.created_at, self.password_hash))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

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
        # Link records sit apart, so the accounts directory holds only people. Made on the first one.
        self._links = self._dir / "links"
        self.protected = resolve_floor(admin_name)
        self._ignores_case = _ignores_case(self._dir)
        _dummy_hash()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(
        self,
        username: str,
        password: str,
        *,
        groups: builtins.list[str] | None = None,
        email: str = "",
        phone: str = "",
        name: str = "",
        org: str = "",
        change_required: bool = False,
        email_verified: bool | None = None,
    ) -> Account:
        """Create a new account.

        Args:
            username: Unique account name.
            password: Plaintext password -- bcrypt-hashed before storage.
            groups: Optional list of group memberships.
            email: Optional contact email.
            phone: Optional contact phone.
            name: Optional display name (distinct from ``username``).
            org: The registered org the account belongs to, empty for none.
            change_required: The password is issued, not the owner's own, so the
                owner must replace it before the API serves them.
            email_verified: Whether *email* is vouched for (:attr:`Account.email_verified`).

        Returns:
            The newly created Account.

        Raises:
            AccountExistsError: An account is already stored under *username*,
                including one another replica created at the same moment, or under
                it in another case.
            InvalidNameError: *username* is not a name an account can have.
            ValueError: bcrypt refuses *password*.
        """
        if not VALID_NAME.match(username):
            raise InvalidNameError(f"Invalid account name: {username!r}")
        path = self._path(username)
        if (taken := self.taken_name(username)) is not None:
            raise AccountExistsError(f"Account already exists: {taken}")
        account = _new_account(
            username,
            password,
            groups=groups,
            email=email,
            phone=phone,
            name=name,
            org=org,
            change_required=change_required,
            email_verified=email_verified,
        )
        try:
            self._write_new(path, account)
        except FileExistsError as exc:
            raise AccountExistsError(f"Account already exists: {username}") from exc
        return account

    def taken_name(self, username: str) -> str | None:
        """The stored account or link record a new account named *username* clashes with, matched ignoring case. An invalid name clashes with none."""
        if not VALID_NAME.match(username):
            return None
        for directory in (self._dir, self._links):
            if (twin := case_twin(directory, username)) is not None:
                return twin
        # case_twin reports only another case, so the exact name is looked up in both directories.
        name = self._path(username).name
        if ((self._dir / name).exists()) or ((self._links / name).exists()):
            return username
        return None

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
            InvalidNameError: The username is not a safe filename stem.
            ProtectedAccountError: The record would leave a recovery credential
                unable to log in.
        """
        if not VALID_NAME.match(account.username):
            raise InvalidNameError(f"Invalid account name: {account.username!r}")
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
        try:
            path = self._stored(username)
        except KeyError:
            return None
        if path is None:
            return None
        return self._read(path)

    def list(self) -> builtins.list[Account]:
        """Return all accounts in the store.

        Returns:
            List of Account objects, ordered by filename. An account deleted while
            the list is read is absent from it.
        """
        return builtins.list(self.scan())

    def scan(self) -> Iterator[Account]:
        """Yield accounts one file at a time, so a caller that stops early reads no more.

        A file deleted between the directory listing and its read (a soft-delete
        renames the account) is skipped, not raised.

        Yields:
            Each stored Account, ordered by filename.
        """
        paths = [*self._dir.glob("*.yaml"), *self._links.glob("*.yaml")]
        for path in sorted(paths, key=_file_name):
            try:
                yield self._read(path)
            except FileNotFoundError:
                continue

    def update(self, username: str, *, allow_protected: bool = False, **fields: object) -> Account:
        """Update mutable fields on an existing account.

        Permitted fields: ``enabled``, ``blocked``, ``disabled_at``,
        ``blocked_at``, ``groups``, ``email``, ``phone``, ``name``, ``org``, plus
        the external-identity stamps and links. Updating ``username`` or ``password_hash``
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
        path = self._stored(username)
        if path is None:
            raise KeyError(username)

        account = self._read(path)
        if not allow_protected:
            self.protected.check_account_update(username, fields, account.groups)
        updates = _access_updates(account, fields)
        account = account.model_copy(update={**updates, "updated_at": _now()})
        self._write(path, account)
        return account

    def record_sign_in(self, username: str) -> None:
        """Stamp ``last_login_at`` with now and leave ``updated_at``, since a sign-in changes nothing on the account. An unknown username records nothing."""
        path = self._stored(username)
        if path is None:
            return
        account = self._read(path)
        self._write(path, account.model_copy(update={"last_login_at": _now()}))

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
        path = self._stored(username)
        if path is None:
            raise KeyError(username)

        account = self._read(path)
        account = account.model_copy(
            update=_password_update(account, new_password, change_required)
        )
        self._write(path, account)

    def end_sessions(self, username: str) -> Account:
        """End every session of an account: each token minted for it before now is refused.

        Args:
            username: Account whose sessions end.

        Returns:
            The updated Account.

        Raises:
            KeyError: If no account with *username* exists.
        """
        path = self._stored(username)
        if path is None:
            raise KeyError(username)

        account = self._read(path)
        account = account.model_copy(update=_sessions_ended(account))
        self._write(path, account)
        return account

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
        path = self._stored(username)
        if path is None:
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
        path = self._stored(username)
        if path is None:
            raise KeyError(username)
        if not allow_protected:
            self.protected.check_account_delete(username)
        path.unlink()

    def verify_password(self, username: str, password: str) -> bool:
        """Check whether *password* matches the stored hash.

        Always performs a bcrypt check -- even for unknown users -- to
        prevent timing-based username enumeration.

        Args:
            username: Account name.
            password: Plaintext password to verify.

        Returns:
            True if the password matches, False otherwise.
        """
        return self.verify_account_password(self.get(username), password)

    @staticmethod
    def verify_account_password(account: Account | None, password: str) -> bool:
        """Check *password* against the hash on *account*, a record the caller already holds.

        Reads nothing, so the password is judged against the same record as the
        caller's other checks. Every path runs one bcrypt check: a missing account,
        an empty password, and an account with no usable hash (the external
        sentinel) are checked against the dummy hash and refused.

        Args:
            account: The stored record, or None when there is none.
            password: Plaintext password to verify.

        Returns:
            True if the password matches, False otherwise.
        """
        if account is None or not password or not account.password_hash.startswith("$2"):
            _checkpw(password, _dummy_hash())
            return False
        return _checkpw(password, account.password_hash.encode("utf-8"))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _path(self, username: str) -> Path:
        """The file *username*'s account lives in.

        Raises:
            KeyError: No account can hold *username*, so it is looked up nowhere.
        """
        return self._dir / f"{store_key(username)}.yaml"

    def _home(self, *, account: Account) -> Path:
        """The file *account* belongs in: under links/ for a link record, beside the people otherwise."""
        directory = self._links if account.is_link_record else self._dir
        return directory / f"{store_key(account.username)}.yaml"

    def _stored(self, username: str) -> Path | None:
        """The file holding the account stored under exactly *username*, a person's or a link record's, or None.

        Raises:
            KeyError: No account can hold *username*, so it is looked up nowhere.
        """
        name = self._path(username).name
        for directory in (self._dir, self._links):
            path = directory / name
            if not (path.exists()):
                continue
            # A case-blind filesystem opens bob.yaml for Bob, so the entry's own name must match.
            if self._ignores_case and name not in os.listdir(directory):
                continue
            return path
        return None

    def _read(self, path: Path) -> Account:
        """Load an Account from a YAML file.

        The username is derived from the filename stem -- it is not stored
        inside the YAML body. Parses are reused while the file is unchanged
        (:func:`yaml_load_cached`), so scanning N accounts costs N stats, not N parses.
        """
        data: dict = yaml_load_cached(path)
        data["username"] = path.stem
        return Account.model_validate(data)

    def _write(self, path: Path, account: Account) -> None:
        """Persist an Account to YAML, omitting the username field, moving it from *path* when it became or stopped being a link record."""
        home = self._home(account=account)
        home.parent.mkdir(exist_ok=True)
        data = account.model_dump(exclude={"username"})
        yaml_dump(data, home)
        if home != path:
            path.unlink(missing_ok=True)

    def _write_new(self, path: Path, account: Account) -> None:
        """Persist a new Account to *path*, which the filesystem refuses if any file holds it.

        Exclusive-create mode makes the existence check and the create one step, so of
        two replicas creating one account at once exactly one succeeds.

        Raises:
            FileExistsError: A file already holds *path*. It is left untouched.
        """
        text = yaml_dump_string(account.model_dump(exclude={"username"}), target=str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "x", encoding="utf-8", newline="\n")
        try:
            with f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            # A partial file would read as a broken account under this name.
            path.unlink(missing_ok=True)
            raise


# Fields update() may change. username and password_hash are excluded (the
# password changes only via reset_password).
_UPDATABLE_FIELDS = (
    "enabled",
    "blocked",
    "disabled_at",
    "blocked_at",
    "groups",
    "external",
    "source_provider",
    "external_id",
    "subject",
    "oidc_id",
    "last_login_at",
    "email",
    "phone",
    "name",
    "org",
    "email_verified",
    "linked_to",
    "identities",
)


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _file_name(path: Path) -> str:
    """The sort key that orders account files by name across both directories."""
    return path.name


def discard_created(store: AccountStore, created: Account) -> None:
    """Remove *created*, an account the caller's own create stored and could not finish.

    Only that record goes: an account stored under the name since, with another
    creation time, is left alone. A failure to remove it is logged, never raised,
    because the caller re-raises the error that stopped it.

    Args:
        store: The store the create wrote to.
        created: The account that create returned.
    """
    try:
        stored = store.get(created.username)
        if stored is None or stored.created_at != created.created_at:
            return
        store.delete(created.username)
    except KeyError:
        return
    except Exception as exc:
        logger.error(
            "Could not remove an account whose create did not finish",
            username=created.username,
            error=type(exc).__name__,
        )


def delete_link_records(store: AccountStore, account: Account) -> None:
    """Delete the link record of each sign-in linked to *account*, so none outlives it."""
    for identity in account.identities:
        record = store.get(identity.key)
        if (record is not None) and (record.linked_to == account.username):
            store.delete(identity.key)


def anonymized_soft_delete_username() -> str:
    """A one-use account name: a UUID with ``-deleted`` as the trailing marker.

    Account names must start with an alphanumeric (:data:`VALID_NAME`), so the
    delete marker cannot lead. It trails the UUID as ``{uuid}-deleted``.
    """
    return f"{uuid.uuid4()}-deleted"


def soft_delete_account(store: AccountStore, username: str) -> Account:
    """Disable *username* and rename it to an anonymized ``{uuid}-deleted`` id.

    Clears contact and subject fields and the linked sign-ins, whose link records
    are deleted, so the record no longer identifies the person. Ownership stamps
    (``source_provider``, ``oidc_id``) stay for audit.

    Raises:
        KeyError: No account is stored under *username*.
        ProtectedAccountError: *username* is a recovery credential.
    """
    existing = store.get(username)
    if existing is None:
        raise KeyError(username)
    # Refuse before writing anything under a new name.
    store.protected.check_account_update(username, {"enabled": False}, existing.groups)

    anonymized = anonymized_soft_delete_username()
    stamped = _apply_access_stamps({"enabled": False})
    rewritten = existing.model_copy(
        update={
            "username": anonymized,
            "previous_username": username,
            "enabled": stamped["enabled"],
            "disabled_at": stamped["disabled_at"],
            "email": "",
            "phone": "",
            "name": "",
            "subject": "",
            "attributes": {},
            "identities": [],
            **_session_end(existing),
            "updated_at": _now(),
        }
    )
    store.put(rewritten)
    # Old key must go. The account now lives only under the anonymized name.
    store.delete(username)
    delete_link_records(store, existing)
    return rewritten


def later_session_end(durable: Account, live: Account) -> Account:
    """*durable*, carrying whichever of the two copies' session ends is later.

    A *live* copy that has ended its sessions more times than *durable* keeps its
    epoch, and keeps a disable or block it carries. Two copies at the same count with
    different epochs cannot be ordered, so every session ends again.

    Args:
        durable: The account as the deploy repo holds it.
        live: The same account as the live store holds it.

    Returns:
        *durable* with the session state that refuses every token either copy refuses.
    """
    if live.session_generation > durable.session_generation:
        update: dict[str, object] = {
            "session_epoch": live.session_epoch,
            "session_generation": live.session_generation,
        }
        if durable.enabled and not live.enabled:
            update |= {"enabled": False, "disabled_at": live.disabled_at}
        if live.blocked and not durable.blocked:
            update |= {"blocked": True, "blocked_at": live.blocked_at}
        return durable.model_copy(update=update)
    unordered = live.session_generation == durable.session_generation
    if unordered and live.session_epoch != durable.session_epoch:
        return durable.model_copy(update=_session_end(durable))
    return durable


def _new_session_epoch() -> str:
    """A fresh session epoch. No token minted before it carries a marker made from it."""
    return secrets.token_urlsafe(16)


def _session_end(account: Account) -> dict[str, object]:
    """The fields that end every session of *account*, counted so its copies can be ordered."""
    return {
        "session_epoch": _new_session_epoch(),
        "session_generation": account.session_generation + 1,
    }


def _sessions_ended(account: Account) -> dict[str, object]:
    """The fields that end every session of *account*, stamped."""
    return {**_session_end(account), "updated_at": _now()}


def _access_updates(account: Account, fields: dict[str, object]) -> dict[str, object]:
    """The permitted *fields* of an update to *account*, stamped.

    A change to ``enabled`` or ``blocked`` also ends every session, so a token minted
    before an account was disabled does not come back when it is enabled again.
    """
    updates = _apply_access_stamps({k: fields[k] for k in _UPDATABLE_FIELDS if k in fields})
    access = ("enabled", "blocked")
    if any(key in updates and updates[key] != getattr(account, key) for key in access):
        updates.update(_session_end(account))
    return updates


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
    org: str,
    change_required: bool,
    email_verified: bool | None,
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
        email_verified=email_verified,
        phone=phone,
        name=name,
        org=org,
        created_at=now,
        updated_at=now,
        password_change_required=change_required,
        seeded_password_hash=digest if change_required else "",
    )


def _password_update(
    account: Account, new_password: str, change_required: bool
) -> dict[str, object]:
    """The fields a password reset of *account* writes, the seeded digest too when issued.

    The reset ends every session as well, so a restore that puts back the hash it
    replaced never revives a token minted under that hash.
    """
    digest = hash_password(new_password)
    update: dict[str, object] = {
        **_sessions_ended(account),
        "password_hash": digest,
        "password_change_required": change_required,
    }
    if change_required:
        update["seeded_password_hash"] = digest
    return update


def hash_password(password: str, rounds: int = BCRYPT_ROUNDS) -> str:
    """Bcrypt-hash *password* and return the hash as a UTF-8 string."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=rounds)).decode("utf-8")


def matches_digest(password: str, digest: str) -> bool:
    """Whether *digest* was made from *password*. An empty or non-bcrypt digest never matches."""
    if not password or not digest.startswith("$2"):
        return False
    return _checkpw(password, digest.encode("utf-8"))


def refuse_unhashable_password(password: str) -> str:
    """Return *password* when bcrypt can hash it.

    Raises:
        ValueError: *password* is longer than :data:`BCRYPT_MAX_PASSWORD_BYTES` in UTF-8.
    """
    if len(password.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError(f"at most {BCRYPT_MAX_PASSWORD_BYTES} bytes, the most bcrypt hashes")
    return password


def _checkpw(password: str, digest: bytes) -> bool:
    """Check *password* against *digest* with bcrypt, never raising for a long candidate.

    bcrypt refuses more than 72 bytes rather than truncating, and the first 72 alone must
    never authenticate, so a longer candidate is checked truncated for equal cost and fails.
    """
    candidate = password.encode("utf-8")
    if len(candidate) > BCRYPT_MAX_PASSWORD_BYTES:
        bcrypt.checkpw(candidate[:BCRYPT_MAX_PASSWORD_BYTES], digest)
        return False
    return bcrypt.checkpw(candidate, digest)


def _now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()
