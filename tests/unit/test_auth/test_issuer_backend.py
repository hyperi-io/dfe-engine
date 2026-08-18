#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_issuer_backend.py
#  Purpose:      Tests for the issuer-agnostic backend + dex gRPC implementation
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for the issuer management plane.

Exercises the dex backend's request construction, response mapping, partial-
update field preservation, bcrypt hashing and connector JSON round-trip against
a fake stub (no gRPC channel), plus the factory's enable/disable gating and the
DFE_ISSUER_* config cascade. No dex instance required.
"""

from __future__ import annotations

import bcrypt
import grpc
import pytest
from grpc import aio

from dfe_engine.auth.issuer.backend import (
    IssuerConnector,
    IssuerError,
    IssuerSession,
    IssuerUser,
)
from dfe_engine.auth.issuer.dex.client import DexIssuerBackend, _dump_config, _load_config
from dfe_engine.auth.issuer.dex.proto import api_pb2 as pb

# Default response per RPC; a test overrides via stub.responses[name].
_DEFAULT_RESPONSES = {
    "CreatePassword": pb.CreatePasswordResp(already_exists=False),
    "UpdatePassword": pb.UpdatePasswordResp(not_found=False),
    "DeletePassword": pb.DeletePasswordResp(not_found=False),
    "ListPasswords": pb.ListPasswordResp(),
    "VerifyPassword": pb.VerifyPasswordResp(verified=True),
    "CreateConnector": pb.CreateConnectorResp(already_exists=False),
    "UpdateConnector": pb.UpdateConnectorResp(not_found=False),
    "DeleteConnector": pb.DeleteConnectorResp(not_found=False),
    "ListConnectors": pb.ListConnectorResp(),
    "ListRefresh": pb.ListRefreshResp(),
    "RevokeRefresh": pb.RevokeRefreshResp(not_found=False),
    "GetVersion": pb.VersionResp(server="v2.45.0", api=2),
}


class FakeDexStub:
    """Stands in for the generated DexStub: records the last request per RPC and
    returns a configured (or default) response. RPC names are resolved through
    __getattr__ so the fake matches dex's PascalCase methods without redefining
    each one (and without tripping the lowercase-name lint)."""

    def __init__(self) -> None:
        self.requests: dict[str, object] = {}
        self.responses: dict[str, object] = {}
        self.raise_on: str | None = None

    def __getattr__(self, name: str):
        if name not in _DEFAULT_RESPONSES:
            raise AttributeError(name)

        async def rpc(request):
            self.requests[name] = request
            if self.raise_on == name:
                raise aio.AioRpcError(
                    grpc.StatusCode.UNAVAILABLE, aio.Metadata(), aio.Metadata(), details="boom"
                )
            return self.responses.get(name, _DEFAULT_RESPONSES[name])

        return rpc


def _backend_with(stub: FakeDexStub) -> DexIssuerBackend:
    """A backend whose channel is bypassed by pre-seeding the fake stub."""
    backend = DexIssuerBackend(
        endpoint="dex:5557", ca_cert="/x/ca", client_cert="/x/crt", client_key="/x/key"
    )
    backend._stub = stub  # bypass _ensure_stub (no real channel)
    return backend


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class TestUsers:
    async def test_create_user_hashes_password_and_defaults_username(self):
        stub = FakeDexStub()
        backend = _backend_with(stub)

        created = await backend.create_user(email="a@b.co", password="s3cret")

        assert created is True
        req = stub.requests["CreatePassword"]
        assert req.password.email == "a@b.co"
        assert req.password.username == "a@b.co"  # defaults to email
        # Stored hash is bcrypt of the plaintext, not the plaintext.
        assert req.password.hash != b"s3cret"
        assert bcrypt.checkpw(b"s3cret", req.password.hash)

    async def test_create_user_reports_already_exists_as_false(self):
        stub = FakeDexStub()
        stub.responses["CreatePassword"] = pb.CreatePasswordResp(already_exists=True)
        backend = _backend_with(stub)

        assert await backend.create_user(email="a@b.co", password="x") is False

    async def test_update_user_password_only_preserves_username(self):
        # dex UpdatePassword overwrites username too, so a password-only change
        # must carry the existing username forward -- hence the lookup.
        stub = FakeDexStub()
        stub.responses["ListPasswords"] = pb.ListPasswordResp(
            passwords=[pb.Password(email="a@b.co", username="keep", hash=b"old")]
        )
        backend = _backend_with(stub)

        ok = await backend.update_user(email="a@b.co", new_password="new")

        assert ok is True
        req = stub.requests["UpdatePassword"]
        assert bcrypt.checkpw(b"new", req.new_hash)
        assert req.new_username == "keep"

    async def test_update_user_password_only_missing_returns_false(self):
        stub = FakeDexStub()  # empty ListPasswords
        backend = _backend_with(stub)

        assert await backend.update_user(email="ghost@b.co", new_password="x") is False

    async def test_update_user_username_only_preserves_existing_hash(self):
        stub = FakeDexStub()
        existing_hash = bcrypt.hashpw(b"keepme", bcrypt.gensalt())
        stub.responses["ListPasswords"] = pb.ListPasswordResp(
            passwords=[pb.Password(email="a@b.co", hash=existing_hash, username="old")]
        )
        backend = _backend_with(stub)

        ok = await backend.update_user(email="a@b.co", new_username="new")

        assert ok is True
        req = stub.requests["UpdatePassword"]
        assert req.new_username == "new"
        assert req.new_hash == existing_hash  # hash preserved, not blanked

    async def test_update_user_username_only_missing_returns_false(self):
        stub = FakeDexStub()  # empty ListPasswords
        backend = _backend_with(stub)

        assert await backend.update_user(email="ghost@b.co", new_username="x") is False

    async def test_update_user_noop_returns_true_without_calling(self):
        stub = FakeDexStub()
        backend = _backend_with(stub)

        assert await backend.update_user(email="a@b.co") is True
        assert stub.requests == {}

    async def test_delete_user_maps_not_found(self):
        stub = FakeDexStub()
        stub.responses["DeletePassword"] = pb.DeletePasswordResp(not_found=True)
        backend = _backend_with(stub)

        assert await backend.delete_user(email="a@b.co") is False

    async def test_list_users_maps_subject_from_user_id(self):
        stub = FakeDexStub()
        stub.responses["ListPasswords"] = pb.ListPasswordResp(
            passwords=[pb.Password(email="a@b.co", username="a", user_id="sub-1")]
        )
        backend = _backend_with(stub)

        users = await backend.list_users()

        assert users == [IssuerUser(email="a@b.co", username="a", subject="sub-1")]

    async def test_verify_user_true_only_when_verified_and_found(self):
        stub = FakeDexStub()
        backend = _backend_with(stub)
        assert await backend.verify_user(email="a@b.co", password="x") is True

        stub.responses["VerifyPassword"] = pb.VerifyPasswordResp(verified=False, not_found=True)
        assert await backend.verify_user(email="a@b.co", password="x") is False


# ---------------------------------------------------------------------------
# Connectors
# ---------------------------------------------------------------------------


class TestConnectors:
    async def test_create_connector_serialises_config_to_json_bytes(self):
        stub = FakeDexStub()
        backend = _backend_with(stub)

        await backend.create_connector(
            id="ldap", type="ldap", name="Corp", config={"host": "ldap.corp", "port": 636}
        )

        cfg = stub.requests["CreateConnector"].connector.config
        assert _load_config(cfg) == {"host": "ldap.corp", "port": 636}

    async def test_list_connectors_parses_config_json(self):
        stub = FakeDexStub()
        stub.responses["ListConnectors"] = pb.ListConnectorResp(
            connectors=[pb.Connector(id="ldap", type="ldap", name="Corp", config=b'{"host":"x"}')]
        )
        backend = _backend_with(stub)

        conns = await backend.list_connectors()

        assert conns == [IssuerConnector(id="ldap", type="ldap", name="Corp", config={"host": "x"})]

    async def test_update_connector_partial_preserves_other_fields(self):
        stub = FakeDexStub()
        stub.responses["ListConnectors"] = pb.ListConnectorResp(
            connectors=[pb.Connector(id="ldap", type="ldap", name="Old", config=b'{"a":1}')]
        )
        backend = _backend_with(stub)

        await backend.update_connector(id="ldap", new_name="New")

        req = stub.requests["UpdateConnector"]
        assert req.new_name == "New"
        assert req.new_type == "ldap"  # preserved
        assert req.new_config == b'{"a":1}'  # preserved


# ---------------------------------------------------------------------------
# Sessions + lifecycle + errors
# ---------------------------------------------------------------------------


class TestSessionsAndLifecycle:
    async def test_list_sessions_keys_on_subject(self):
        stub = FakeDexStub()
        stub.responses["ListRefresh"] = pb.ListRefreshResp(
            refresh_tokens=[pb.RefreshTokenRef(id="r1", client_id="dfe-envoy", created_at=5)]
        )
        backend = _backend_with(stub)

        sessions = await backend.list_sessions(subject="sub-1")

        assert stub.requests["ListRefresh"].user_id == "sub-1"
        assert sessions == [IssuerSession(id="r1", client_id="dfe-envoy", created_at=5)]

    async def test_revoke_session_maps_not_found(self):
        stub = FakeDexStub()
        stub.responses["RevokeRefresh"] = pb.RevokeRefreshResp(not_found=True)
        backend = _backend_with(stub)

        assert await backend.revoke_session(subject="s", client_id="c") is False

    async def test_health_returns_server_and_api(self):
        backend = _backend_with(FakeDexStub())
        assert await backend.health() == ("v2.45.0", 2)

    async def test_transport_error_becomes_issuer_error(self):
        stub = FakeDexStub()
        stub.raise_on = "CreatePassword"
        backend = _backend_with(stub)

        with pytest.raises(IssuerError):
            await backend.create_user(email="a@b.co", password="x")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


class TestConfigHelpers:
    def test_dump_none_is_empty_object(self):
        assert _dump_config(None) == b"{}"

    def test_load_empty_bytes_is_empty_dict(self):
        assert _load_config(b"") == {}

    def test_load_non_object_json_is_empty_dict(self):
        assert _load_config(b"[1,2]") == {}

    def test_load_invalid_json_is_empty_dict(self):
        assert _load_config(b"not json") == {}

    def test_round_trip(self):
        assert _load_config(_dump_config({"k": "v"})) == {"k": "v"}


# ---------------------------------------------------------------------------
# Factory + settings cascade
# ---------------------------------------------------------------------------


class TestFactory:
    def test_disabled_returns_none(self):
        from dfe_engine.auth.issuer.factory import build_issuer_backend
        from dfe_engine.settings import DFESettings

        settings = DFESettings(env="dev")  # issuer disabled by default
        assert build_issuer_backend(settings) is None

    def test_enabled_but_incomplete_returns_none(self):
        from dfe_engine.auth.issuer.factory import build_issuer_backend
        from dfe_engine.settings import DFESettings

        settings = DFESettings(env="dev")
        settings.issuer.enabled = True
        settings.issuer.endpoint = "dex:5557"  # missing cert material
        assert build_issuer_backend(settings) is None

    def test_enabled_and_complete_builds_dex_backend(self):
        from dfe_engine.auth.issuer.factory import build_issuer_backend
        from dfe_engine.settings import DFESettings

        settings = DFESettings(env="dev")
        settings.issuer.enabled = True
        settings.issuer.endpoint = "dex:5557"
        settings.issuer.ca_cert = "/x/ca"
        settings.issuer.client_cert = "/x/crt"
        settings.issuer.client_key = "/x/key"
        backend = build_issuer_backend(settings)
        assert isinstance(backend, DexIssuerBackend)


class TestSettingsCascade:
    def test_dfe_issuer_env_maps_onto_issuer_block(self, monkeypatch):
        from dfe_engine.settings import load_settings

        monkeypatch.setenv("DFE_ISSUER_ENABLED", "true")
        monkeypatch.setenv("DFE_ISSUER_ENDPOINT", "dfe-dex.dex:5557")
        monkeypatch.setenv("DFE_ISSUER_CA_CERT", "/etc/issuer/ca.crt")
        monkeypatch.setenv("DFE_ISSUER_CLIENT_CERT", "/etc/issuer/tls.crt")
        monkeypatch.setenv("DFE_ISSUER_CLIENT_KEY", "/etc/issuer/tls.key")
        monkeypatch.setenv("DFE_ISSUER_SERVER_NAME", "dfe-dex.dex.svc")

        settings = load_settings()

        assert settings.issuer.enabled is True
        assert settings.issuer.endpoint == "dfe-dex.dex:5557"
        assert settings.issuer.ca_cert == "/etc/issuer/ca.crt"
        assert settings.issuer.client_cert == "/etc/issuer/tls.crt"
        assert settings.issuer.client_key == "/etc/issuer/tls.key"
        assert settings.issuer.server_name == "dfe-dex.dex.svc"
