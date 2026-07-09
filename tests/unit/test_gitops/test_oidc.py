"""Tests for the Envoy OIDC values renderer."""

from __future__ import annotations

from dfe_engine.gitops.oidc import build_oidc_providers, render_envoy_oidc_values
from dfe_engine.yaml_utils import yaml_load_string


def test_render_disabled_when_no_providers() -> None:
    out = yaml_load_string(render_envoy_oidc_values([]))
    assert out["oidc"]["enabled"] is False
    assert out["oidc"]["providers"] == []
    assert out["jwtAuthn"]["enabled"] is False
    assert "issuer" not in out["jwtAuthn"]


def test_render_enabled_maps_providers() -> None:
    out = yaml_load_string(
        render_envoy_oidc_values(
            [
                {
                    "name": "google-workspace",
                    "issuer": "https://accounts.google.com",
                    "client_id": "abc",
                }
            ]
        )
    )
    assert out["oidc"]["enabled"] is True
    assert out["oidc"]["secretStoreName"] == "dfe-secret-store"
    p = out["oidc"]["providers"][0]
    assert p["name"] == "google-workspace"
    assert p["issuerUrl"] == "https://accounts.google.com"
    assert p["clientId"] == "abc"
    assert p["clientSecretName"] == "dfe-oidc-google-workspace"
    assert p["clientSecretKey"] == "client-secret"
    assert out["jwtAuthn"]["issuer"] == "https://accounts.google.com"


class _FakeProvider:
    def __init__(self, issuer: str, client_id_env: str, enabled: bool = True) -> None:
        self.issuer = issuer
        self.client_id_env = client_id_env
        self.enabled = enabled


class _FakeRegistry:
    def __init__(self, items: list[tuple[str, _FakeProvider]]) -> None:
        self._items = items

    def list(self) -> list[tuple[str, _FakeProvider]]:
        return self._items


def test_build_providers_filters_disabled_and_resolves_client_id(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_CID", "cid-123")
    reg = _FakeRegistry(
        [
            ("google", _FakeProvider("https://accounts.google.com", "GOOGLE_CID")),
            ("okta-off", _FakeProvider("https://okta.example", "OKTA_CID", enabled=False)),
        ]
    )
    built = build_oidc_providers(reg)
    assert len(built) == 1
    assert built[0] == {
        "name": "google",
        "issuer": "https://accounts.google.com",
        "client_id": "cid-123",
    }
