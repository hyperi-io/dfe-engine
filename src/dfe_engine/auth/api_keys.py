#  Project:      dfe-engine
#  File:         src/dfe_engine/auth/api_keys.py
#  Purpose:      YAML-backed API key store with SHA-256 hashed tokens
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""API key management for dfe-engine.

API keys use a prefix+short_token+long_token format:

    dfe_ak_{short_token}_{long_token}

- short_token: 8 hex chars stored plaintext for O(1) file lookup
- long_token:  32 hex chars stored as a SHA-384 hash ("sha384:{hex}", CNSA;
  pre-existing "sha256:" keys still verify)

The full key is shown exactly once -- at creation time. Only the hash is
persisted on disk; there is no way to reconstruct the full key from
stored metadata.

Keys may carry an optional ``expires_at`` (ISO-8601, normalised to UTC).
Expiry is enforced at verify() time, so an expired key stops working the
moment it lapses without any sweeper having to run; the file stays on disk
until an admin revokes it, which keeps the lapse visible in list().

Lookup strategy: filename is the key name; short_token is stored inside
the YAML for cross-file lookup during verify()/revoke().
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from dfe_engine.yaml_utils import yaml_dump, yaml_load

_KEY_PREFIX = "dfe_ak"
# API key name becomes the filename stem ({name}.yaml) - reject path traversal.
# \Z (not $) anchors the true end of string so no trailing newline slips through.
_VALID_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}\Z")
_SHORT_BYTES = 4  # 4 bytes -> 8 hex chars
_LONG_BYTES = 16  # 16 bytes -> 32 hex chars


def parse_expiry(value: str) -> datetime:
    """Parse an ISO-8601 expiry into an aware UTC datetime.

    Accepts a trailing ``Z`` and date-only values (midnight UTC). A naive
    value is read as UTC rather than local time, so the same string means the
    same instant on every host.

    Raises:
        ValueError: If the value is not a parseable ISO-8601 timestamp.
    """
    text = value.strip()
    if not text:
        raise ValueError("Invalid expires_at: empty value")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid expires_at: {value!r} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class APIKey(BaseModel):
    """Stored metadata for an API key.

    The full plaintext long token is NEVER stored here -- only the hash.
    """

    name: str
    short_token: str
    key_hash: str  # "sha384:{hex}" (CNSA); legacy "sha256:{hex}" still verifies
    enabled: bool = True
    groups: list[str] = Field(default_factory=list)
    description: str = ""
    created_at: str = ""
    expires_at: str | None = None  # ISO-8601 UTC; None = never expires

    def is_expired(self, now: datetime | None = None) -> bool:
        """True when the key carries an expiry that has already passed.

        An unparseable stored expiry counts as expired -- a corrupted or
        hand-edited value must not silently grant an unbounded key.
        """
        if not self.expires_at:
            return False
        try:
            expiry = parse_expiry(self.expires_at)
        except ValueError:
            return True
        return expiry <= (now or datetime.now(UTC))


class APIKeyStore:
    """YAML-backed store for API keys.

    Each key lives in its own ``{name}.yaml`` file inside ``keys_dir``.
    The directory is created at init time if it does not exist.
    """

    def __init__(self, keys_dir: Path) -> None:
        self._keys_dir = Path(keys_dir)
        self._keys_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(
        self,
        name: str,
        *,
        groups: list[str] | None = None,
        description: str = "",
        expires_at: str | None = None,
    ) -> tuple[APIKey, str]:
        """Create a new API key.

        Args:
            name: Unique human-readable key name (used as filename stem).
            groups: RBAC groups to associate with the key.
            description: Optional human-readable description.
            expires_at: Optional ISO-8601 expiry, normalised to UTC before
                storage. None (the default) means the key never expires.

        Returns:
            Tuple of (APIKey metadata, full_key_string). The full key is
            shown exactly once and cannot be recovered from metadata.

        Raises:
            ValueError: If a key with this name already exists, or if
                expires_at is unparseable or not in the future.
        """
        if not _VALID_NAME.match(name):
            raise ValueError(f"Invalid API key name: {name!r}")
        expiry: str | None = None
        if expires_at is not None:
            parsed_expiry = parse_expiry(expires_at)
            if parsed_expiry <= datetime.now(UTC):
                raise ValueError(f"Invalid expires_at: {expires_at!r} is in the past")
            expiry = parsed_expiry.isoformat()
        key_file = self._keys_dir / f"{name}.yaml"
        if key_file.exists():
            raise ValueError(f"API key '{name}' already exists")

        short_token = secrets.token_hex(_SHORT_BYTES)
        long_token = secrets.token_hex(_LONG_BYTES)
        key_hash = "sha384:" + hashlib.sha384(long_token.encode()).hexdigest()
        full_key = f"{_KEY_PREFIX}_{short_token}_{long_token}"

        key_meta = APIKey(
            name=name,
            short_token=short_token,
            key_hash=key_hash,
            enabled=True,
            groups=groups or [],
            description=description,
            created_at=datetime.now(UTC).isoformat(),
            expires_at=expiry,
        )

        # Write to disk -- name is the filename stem, not in the YAML body
        data = key_meta.model_dump()
        data.pop("name")  # name is the filename stem
        yaml_dump(data, key_file)

        return key_meta, full_key

    def get(self, name: str) -> APIKey | None:
        """Read key metadata by name.

        Returns None if the key does not exist.
        """
        key_file = self._keys_dir / f"{name}.yaml"
        if not key_file.exists():
            return None
        return self._load_key(key_file)

    def list(self) -> list[APIKey]:
        """Return all stored keys sorted by name."""
        keys = [self._load_key(f) for f in sorted(self._keys_dir.glob("*.yaml"))]
        return keys

    def verify(self, submitted_key: str) -> APIKey | None:
        """Verify a submitted API key.

        Parses the key format, locates the matching file by short_token,
        then performs a timing-safe hash comparison of the long token.

        Args:
            submitted_key: Full key string in ``dfe_ak_{short}_{long}`` format.

        Returns:
            APIKey metadata if valid, enabled and unexpired; None otherwise.
        """
        key_meta, _reason = self.verify_detailed(submitted_key)
        return key_meta

    def verify_detailed(self, submitted_key: str) -> tuple[APIKey | None, str]:
        """Verify a key and report why it failed.

        Same acceptance rules as :meth:`verify`; the extra reason exists so
        callers can audit-log *why* a key was rejected. Status checks
        (disabled/expired) run only AFTER the hash comparison proves the
        caller holds the real key, so a reason can never confirm the
        existence of a key the caller has not already got.

        Returns:
            ``(key_meta, "ok")`` on success, else ``(None, reason)`` where
            reason is one of ``malformed_key``, ``unknown_key``,
            ``invalid_key``, ``disabled_key``, ``expired_key``.
        """
        parsed = self._parse_key(submitted_key)
        if parsed is None:
            return None, "malformed_key"

        short_token, long_token = parsed

        # Locate key file by scanning for matching short_token
        key_meta = self._find_by_short_token(short_token)
        if key_meta is None:
            return None, "unknown_key"

        # Timing-safe comparison. New keys are SHA-384 (CNSA); pre-existing
        # sha256: keys still verify under their stored algorithm.
        algo, _, stored_hash = key_meta.key_hash.partition(":")
        if algo not in ("sha256", "sha384"):
            return None, "invalid_key"
        submitted_hash = hashlib.new(algo, long_token.encode()).hexdigest()
        if not hmac.compare_digest(submitted_hash, stored_hash):
            return None, "invalid_key"

        if not key_meta.enabled:
            return None, "disabled_key"

        if key_meta.is_expired():
            return None, "expired_key"

        return key_meta, "ok"

    def revoke(self, short_token: str) -> None:
        """Delete a key by its short_token.

        Args:
            short_token: The 8-char hex short token to look up.

        Raises:
            KeyError: If no key with this short_token exists.
        """
        key_meta = self._find_by_short_token(short_token)
        if key_meta is None:
            raise KeyError(f"No API key with short_token '{short_token}'")

        key_file = self._keys_dir / f"{key_meta.name}.yaml"
        key_file.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_key(self, path: Path) -> APIKey:
        """Load an APIKey from a YAML file.

        The key name is derived from the filename stem, not stored in YAML.
        """
        data: dict = yaml_load(path)
        data["name"] = path.stem
        return APIKey(**data)

    def _parse_key(self, key: str) -> tuple[str, str] | None:
        """Parse ``dfe_ak_{short}_{long}`` into (short_token, long_token).

        Returns None if the format is invalid.
        """
        if not key:
            return None
        parts = key.split("_", maxsplit=3)
        if len(parts) != 4:
            return None
        prefix, kind, short_token, long_token = parts
        if prefix != "dfe" or kind != "ak":
            return None
        return short_token, long_token

    def _find_by_short_token(self, short_token: str) -> APIKey | None:
        """Scan all key files to find one matching the given short_token."""
        for key_file in self._keys_dir.glob("*.yaml"):
            key_meta = self._load_key(key_file)
            if key_meta.short_token == short_token:
                return key_meta
        return None
