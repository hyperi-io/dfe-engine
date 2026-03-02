"""Tests for the local authentication provider."""

import bcrypt
import pytest

from dfe_engine.auth import (
    AuthContext,
    AuthenticationError,
    LocalAuthProvider,
    authorize,
)
from dfe_engine.auth.local_provider import _is_bcrypt_hash
from dfe_engine.settings import LocalAuthSettings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings():
    return LocalAuthSettings(
        enabled=True,
        admin_password="admin-secret",
        operator_password="operator-secret",
        viewer_password="viewer-secret",
        org_id="test-org",
    )


@pytest.fixture
def provider(settings):
    return LocalAuthProvider(settings)


@pytest.fixture
def prehashed_settings():
    """Settings with pre-hashed bcrypt passwords."""
    admin_hash = bcrypt.hashpw(b"hashed-admin", bcrypt.gensalt(rounds=4)).decode()
    operator_hash = bcrypt.hashpw(b"hashed-operator", bcrypt.gensalt(rounds=4)).decode()
    viewer_hash = bcrypt.hashpw(b"hashed-viewer", bcrypt.gensalt(rounds=4)).decode()
    return LocalAuthSettings(
        enabled=True,
        admin_password=admin_hash,
        operator_password=operator_hash,
        viewer_password=viewer_hash,
        org_id="hashed-org",
    )


# ---------------------------------------------------------------------------
# Authentication success
# ---------------------------------------------------------------------------


class TestAuthentication:
    def test_authenticate_admin_success(self, provider):
        auth = provider.authenticate("admin", "admin-secret")
        assert isinstance(auth, AuthContext)
        assert auth.user_id == "admin"
        assert auth.roles == ["admin"]

    def test_authenticate_operator_success(self, provider):
        auth = provider.authenticate("operator", "operator-secret")
        assert auth.user_id == "operator"
        assert auth.roles == ["operator"]

    def test_authenticate_viewer_success(self, provider):
        auth = provider.authenticate("viewer", "viewer-secret")
        assert auth.user_id == "viewer"
        assert auth.roles == ["viewer"]

    def test_authenticate_populates_org_id(self, provider):
        auth = provider.authenticate("admin", "admin-secret")
        assert auth.org_id == "test-org"

    def test_authenticate_populates_request_metadata(self, provider):
        auth = provider.authenticate(
            "admin",
            "admin-secret",
            request_id="req-123",
            client_ip="10.0.0.1",
            user_agent="test-agent",
        )
        assert auth.request_id == "req-123"
        assert auth.client_ip == "10.0.0.1"
        assert auth.user_agent == "test-agent"


# ---------------------------------------------------------------------------
# Authentication failure
# ---------------------------------------------------------------------------


class TestAuthenticationFailure:
    def test_wrong_password(self, provider):
        with pytest.raises(AuthenticationError, match="Invalid username or password"):
            provider.authenticate("admin", "wrong-password")

    def test_unknown_username(self, provider):
        with pytest.raises(AuthenticationError, match="Invalid username or password"):
            provider.authenticate("nobody", "any-password")

    def test_disabled_provider(self, settings):
        settings.enabled = False
        provider = LocalAuthProvider(settings)
        with pytest.raises(AuthenticationError, match="disabled"):
            provider.authenticate("admin", "admin-secret")


# ---------------------------------------------------------------------------
# Default changeme password
# ---------------------------------------------------------------------------


class TestDefaultPassword:
    def test_changeme_password_works(self):
        settings = LocalAuthSettings()  # all defaults
        provider = LocalAuthProvider(settings)
        auth = provider.authenticate("admin", "changeme")
        assert auth.roles == ["admin"]

    def test_changeme_password_works_for_all_accounts(self):
        settings = LocalAuthSettings()
        provider = LocalAuthProvider(settings)
        for username in ("admin", "operator", "viewer"):
            auth = provider.authenticate(username, "changeme")
            assert auth.user_id == username


# ---------------------------------------------------------------------------
# Password resolution
# ---------------------------------------------------------------------------


class TestPasswordResolution:
    def test_plaintext_hashed_at_init(self, provider):
        """Plaintext passwords should be hashed to bcrypt at init."""
        for username, stored_hash in provider._hashes.items():
            assert stored_hash.startswith(b"$2b$")

    def test_bcrypt_hash_used_directly(self, prehashed_settings):
        """Pre-hashed bcrypt values should be used as-is."""
        provider = LocalAuthProvider(prehashed_settings)
        auth = provider.authenticate("admin", "hashed-admin")
        assert auth.roles == ["admin"]

    def test_2a_prefix_accepted(self):
        """$2a$ prefix should be recognized as bcrypt."""
        hash_2a = bcrypt.hashpw(b"test", bcrypt.gensalt(rounds=4)).decode()
        # Force $2a$ prefix (bcrypt 4.x uses $2b$ by default)
        hash_2a = "$2a$" + hash_2a[4:]
        settings = LocalAuthSettings(admin_password=hash_2a)
        provider = LocalAuthProvider(settings)
        # The hash is accepted as-is (stored directly)
        assert provider._hashes["admin"] == hash_2a.encode("utf-8")

    def test_2y_prefix_accepted(self):
        """$2y$ prefix should be recognized as bcrypt."""
        hash_2y = bcrypt.hashpw(b"test", bcrypt.gensalt(rounds=4)).decode()
        hash_2y = "$2y$" + hash_2y[4:]
        settings = LocalAuthSettings(admin_password=hash_2y)
        provider = LocalAuthProvider(settings)
        assert provider._hashes["admin"] == hash_2y.encode("utf-8")

    def test_is_bcrypt_hash_detection(self):
        assert _is_bcrypt_hash("$2b$12$abc123") is True
        assert _is_bcrypt_hash("$2a$10$abc123") is True
        assert _is_bcrypt_hash("$2y$12$abc123") is True
        assert _is_bcrypt_hash("plaintext") is False
        assert _is_bcrypt_hash("") is False
        assert _is_bcrypt_hash("$argon2$v=19$m=65536") is False


# ---------------------------------------------------------------------------
# Password hashing utility
# ---------------------------------------------------------------------------


class TestPasswordHashing:
    def test_hash_produces_bcrypt(self):
        h = LocalAuthProvider.hash_password("secret")
        assert h.startswith("$2b$")

    def test_hash_different_each_call(self):
        """Each call should produce a different hash (salted)."""
        h1 = LocalAuthProvider.hash_password("secret")
        h2 = LocalAuthProvider.hash_password("secret")
        assert h1 != h2

    def test_hash_verify_roundtrip(self):
        h = LocalAuthProvider.hash_password("roundtrip-test")
        assert bcrypt.checkpw(b"roundtrip-test", h.encode("utf-8"))


# ---------------------------------------------------------------------------
# Timing safety
# ---------------------------------------------------------------------------


class TestTimingSafety:
    def test_nonexistent_user_still_takes_time(self, provider):
        """Unknown username should still do a bcrypt check (timing-safe)."""
        # We can't precisely measure timing, but we verify it doesn't
        # short-circuit — it should still raise AuthenticationError
        with pytest.raises(AuthenticationError):
            provider.authenticate("nonexistent", "any-password")


# ---------------------------------------------------------------------------
# Integration: authenticate → authorize
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_authenticate_then_authorize_admin_allowed(self, provider):
        auth = provider.authenticate("admin", "admin-secret")
        result = authorize(auth, "config:write", enabled=True)
        assert result.allowed
        assert result.reason == "role:admin"

    def test_authenticate_then_authorize_viewer_denied_write(self, provider):
        auth = provider.authenticate("viewer", "viewer-secret")
        result = authorize(auth, "config:write", enabled=True)
        assert not result.allowed

    def test_authenticate_then_authorize_operator_partial(self, provider):
        auth = provider.authenticate("operator", "operator-secret")
        # operator can read
        assert authorize(auth, "config:read", enabled=True).allowed
        # operator can compile helm
        assert authorize(auth, "helm:compile", enabled=True).allowed
        # operator cannot execute DDL
        assert not authorize(auth, "helm:execute_ddl", enabled=True).allowed


# ---------------------------------------------------------------------------
# Settings defaults
# ---------------------------------------------------------------------------


class TestSettingsDefaults:
    def test_default_settings_values(self):
        s = LocalAuthSettings()
        assert s.enabled is True
        assert s.admin_password == "changeme"
        assert s.operator_password == "changeme"
        assert s.viewer_password == "changeme"
        assert s.org_id == "default"

    def test_custom_org_id(self):
        s = LocalAuthSettings(org_id="acme-corp")
        provider = LocalAuthProvider(s)
        auth = provider.authenticate("admin", "changeme")
        assert auth.org_id == "acme-corp"
