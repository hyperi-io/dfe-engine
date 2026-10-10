#  Project:      dfe-engine
#  File:         tests/unit/test_secrets_seam.py
#  Purpose:      The scalo.secrets seam - real file backend, no mocks
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The minted-secret seam against a real scalo.secrets file backend (no mocks)."""

import pytest
from scalo.secrets.exceptions import ProviderError

from dfe_engine.secrets import build_secrets
from dfe_engine.settings import SecretsSettings


def test_file_provider_put_get_delete(tmp_path):
    sec = build_secrets(SecretsSettings(provider="file", path=str(tmp_path)))
    sec.put("ch/groups/soc-ro", "hunter2")
    assert sec.get("ch/groups/soc-ro") == "hunter2"
    assert sec.exists("ch/groups/soc-ro") is True
    # upsert (rotation) must overwrite, not raise
    sec.put("ch/groups/soc-ro", "rotated")
    assert sec.get("ch/groups/soc-ro") == "rotated"
    sec.delete("ch/groups/soc-ro")
    assert sec.exists("ch/groups/soc-ro") is False
    # delete is idempotent
    sec.delete("ch/groups/soc-ro")


def test_missing_secret_raises(tmp_path):
    from scalo.secrets.exceptions import SecretNotFoundError

    sec = build_secrets(SecretsSettings(provider="file", path=str(tmp_path)))
    with pytest.raises(SecretNotFoundError):
        sec.get("does/not/exist")


def test_a_read_after_the_store_stops_answering_raises(tmp_path):
    """A store outage shows on the next read, never as the last value it handed out."""
    sec = build_secrets(SecretsSettings(provider="file", path=str(tmp_path)))
    sec.put("oidc/acme/client_secret", "first")
    assert sec.get("oidc/acme/client_secret") == "first"
    stored = tmp_path / "oidc" / "acme" / "client_secret"
    stored.unlink()
    # Reading a directory fails inside the real file provider, as a store outage does.
    stored.mkdir()

    with pytest.raises(ProviderError):
        sec.get("oidc/acme/client_secret")


def test_a_secret_rotated_outside_the_engine_is_read_fresh(tmp_path):
    """ESO or an operator rotates the value behind the engine's back, so no write clears a cache."""
    sec = build_secrets(SecretsSettings(provider="file", path=str(tmp_path)))
    sec.put("oidc/acme/client_secret", "first")
    assert sec.get("oidc/acme/client_secret") == "first"

    (tmp_path / "oidc" / "acme" / "client_secret").write_text("rotated", encoding="utf-8")

    assert sec.get("oidc/acme/client_secret") == "rotated"
