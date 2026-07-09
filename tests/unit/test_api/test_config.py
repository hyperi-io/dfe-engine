"""Tests for the runtime client-config bootstrap endpoint."""

from __future__ import annotations


def test_client_config_is_public_and_shaped(client):
    # public (no auth) so the UI can load it before login
    resp = client.get("/api/v1/config/client")
    assert resp.status_code == 200
    body = resp.json()
    assert "hyperdx" in body
    assert "enabled" in body["hyperdx"]
    assert body["auth_mode"] in ("jwt", "oidc")
    assert "governed_ops" in body["features"]
    # gitops disabled in test settings -> governed_ops feature off
    assert body["features"]["governed_ops"] is False
