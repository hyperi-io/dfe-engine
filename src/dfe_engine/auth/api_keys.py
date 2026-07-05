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
- long_token:  32 hex chars stored as SHA-256 hash ("sha256:{hex}")

The full key is shown exactly once — at creation time. Only the hash is
persisted on disk; there is no way to reconstruct the full key from
stored metadata.

Lookup strategy: filename is the key name; short_token is stored inside
the YAML for cross-file lookup during verify()/revoke().
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from dfe_engine.yaml_utils import yaml_dump, yaml_load

_KEY_PREFIX = "dfe_ak"
_SHORT_BYTES = 4  # 4 bytes → 8 hex chars
_LONG_BYTES = 16  # 16 bytes → 32 hex chars


class APIKey(BaseModel):
    """Stored metadata for an API key.

    The full plaintext long token is NEVER stored here — only the hash.
    """

    name: str
    short_token: str
    key_hash: str  # "sha256:{hex}"
    enabled: bool = True
    groups: list[str] = Field(default_factory=list)
    description: str = ""
    created_at: str = ""


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
    ) -> tuple[APIKey, str]:
        """Create a new API key.

        Args:
            name: Unique human-readable key name (used as filename stem).
            groups: RBAC groups to associate with the key.
            description: Optional human-readable description.

        Returns:
            Tuple of (APIKey metadata, full_key_string). The full key is
            shown exactly once and cannot be recovered from metadata.

        Raises:
            ValueError: If a key with this name already exists.
        """
        key_file = self._keys_dir / f"{name}.yaml"
        if key_file.exists():
            raise ValueError(f"API key '{name}' already exists")

        short_token = secrets.token_hex(_SHORT_BYTES)
        long_token = secrets.token_hex(_LONG_BYTES)
        key_hash = "sha256:" + hashlib.sha256(long_token.encode()).hexdigest()
        full_key = f"{_KEY_PREFIX}_{short_token}_{long_token}"

        key_meta = APIKey(
            name=name,
            short_token=short_token,
            key_hash=key_hash,
            enabled=True,
            groups=groups or [],
            description=description,
            created_at=datetime.now(UTC).isoformat(),
        )

        # Write to disk — name is the filename stem, not in the YAML body
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
        # Skip empty/truncated leftovers (a crashed non-atomic writer) -
        # _load_key returns None for those rather than crashing.
        keys: list[APIKey] = []
        for f in sorted(self._keys_dir.glob("*.yaml")):
            key_meta = self._load_key(f)
            if key_meta is not None:
                keys.append(key_meta)
        return keys

    def verify(self, submitted_key: str) -> APIKey | None:
        """Verify a submitted API key.

        Parses the key format, locates the matching file by short_token,
        then performs a timing-safe SHA-256 comparison of the long token.

        Args:
            submitted_key: Full key string in ``dfe_ak_{short}_{long}`` format.

        Returns:
            APIKey metadata if valid and enabled, None otherwise.
        """
        parsed = self._parse_key(submitted_key)
        if parsed is None:
            return None

        short_token, long_token = parsed

        # Locate key file by scanning for matching short_token
        key_meta = self._find_by_short_token(short_token)
        if key_meta is None:
            return None

        if not key_meta.enabled:
            return None

        # Timing-safe comparison of the hash
        submitted_hash = hashlib.sha256(long_token.encode()).hexdigest()
        stored_hash = key_meta.key_hash.removeprefix("sha256:")
        if not hmac.compare_digest(submitted_hash, stored_hash):
            return None

        return key_meta

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

    def _load_key(self, path: Path) -> APIKey | None:
        """Load an APIKey from a YAML file.

        The key name is derived from the filename stem, not stored in YAML.

        Returns None when the file parses to nothing - a truncated/empty file
        left by a crashed non-atomic writer parses to None, and that must be
        treated as "no key" instead of raising on ``data["name"]``.
        """
        data = yaml_load(path)
        if not data:
            return None
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
            if key_meta is not None and key_meta.short_token == short_token:
                return key_meta
        return None
