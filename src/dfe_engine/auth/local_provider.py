"""Local authentication provider for simple deploy + break-glass admin.

Three fixed accounts (admin, operator, viewer) configured via settings/env vars.
Passwords support both plaintext (dev) and pre-hashed bcrypt (production).

Usage:
    from dfe_engine.auth import LocalAuthProvider, AuthContext
    from dfe_engine.settings import load_settings

    settings = load_settings()
    provider = LocalAuthProvider(settings.auth.local)

    # Returns AuthContext on success, raises AuthenticationError on failure
    auth = provider.authenticate("admin", "changeme")
    # auth.roles == ["admin"], auth.org_id == "default"
"""

from __future__ import annotations

import logging

from dfe_engine.auth.models import AuthContext, AuthenticationError
from dfe_engine.settings import LocalAuthSettings

logger = logging.getLogger(__name__)

# Bcrypt hash prefixes (covers all common variants)
_BCRYPT_PREFIXES = ("$2b$", "$2a$", "$2y$")

# Default password that triggers a startup warning
_DEFAULT_PASSWORD = "changeme"


class LocalAuthProvider:
    """Local auth for simple deploy. Three fixed accounts from settings."""

    ACCOUNTS: dict[str, dict] = {
        "admin": {"role": "admin"},
        "operator": {"role": "operator"},
        "viewer": {"role": "viewer"},
    }

    def __init__(self, settings: LocalAuthSettings) -> None:
        import bcrypt as _bcrypt

        self._bcrypt = _bcrypt
        self._enabled = settings.enabled
        self._org_id = settings.org_id

        self._hashes: dict[str, bytes] = {
            "admin": self._resolve_hash(settings.admin_password),
            "operator": self._resolve_hash(settings.operator_password),
            "viewer": self._resolve_hash(settings.viewer_password),
        }

        # Warn about default passwords
        for username, pw in [
            ("admin", settings.admin_password),
            ("operator", settings.operator_password),
            ("viewer", settings.viewer_password),
        ]:
            if pw == _DEFAULT_PASSWORD:
                logger.warning(
                    "Local account '%s' uses default password — change in production",
                    username,
                )

    def authenticate(
        self,
        username: str,
        password: str,
        *,
        request_id: str | None = None,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> AuthContext:
        """Verify credentials and return AuthContext.

        Args:
            username: Account name (admin, operator, viewer).
            password: Plaintext password to verify.
            request_id: Optional request correlation ID.
            client_ip: Optional client IP address.
            user_agent: Optional client user agent string.

        Returns:
            AuthContext with org_id, user_id, and roles populated.

        Raises:
            AuthenticationError: If provider is disabled, username unknown,
                or password incorrect.
        """
        if not self._enabled:
            raise AuthenticationError("Local authentication is disabled")

        stored_hash = self._hashes.get(username)
        if stored_hash is None:
            # Timing-safe: still do a bcrypt check against a dummy hash
            # to prevent timing attacks that reveal valid usernames
            self._bcrypt.checkpw(b"dummy", self._dummy_hash())
            raise AuthenticationError("Invalid username or password")

        if not self._bcrypt.checkpw(password.encode("utf-8"), stored_hash):
            raise AuthenticationError("Invalid username or password")

        account = self.ACCOUNTS[username]
        return AuthContext(
            org_id=self._org_id,
            user_id=username,
            roles=[account["role"]],
            request_id=request_id,
            client_ip=client_ip,
            user_agent=user_agent,
        )

    def _resolve_hash(self, value: str) -> bytes:
        """If value is a bcrypt hash, use as-is. Otherwise hash with bcrypt."""
        if _is_bcrypt_hash(value):
            return value.encode("utf-8")
        return self._bcrypt.hashpw(value.encode("utf-8"), self._bcrypt.gensalt(rounds=12))

    def _dummy_hash(self) -> bytes:
        """Return a pre-computed bcrypt hash for timing-safe dummy checks."""
        if not hasattr(self, "_cached_dummy"):
            self._cached_dummy = self._bcrypt.hashpw(b"dummy", self._bcrypt.gensalt(rounds=12))
        return self._cached_dummy

    @staticmethod
    def hash_password(password: str, rounds: int = 12) -> str:
        """Utility: hash a plaintext password with bcrypt.

        Use this to pre-generate hashes for production env vars.

        Args:
            password: Plaintext password.
            rounds: bcrypt cost factor (default 12).

        Returns:
            Bcrypt hash string (e.g. '$2b$12$...').
        """
        import bcrypt

        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=rounds)).decode(
            "utf-8"
        )


def _is_bcrypt_hash(value: str) -> bool:
    """Detect bcrypt hash by prefix."""
    return any(value.startswith(prefix) for prefix in _BCRYPT_PREFIXES)
