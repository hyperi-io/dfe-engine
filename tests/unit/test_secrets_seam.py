#  Project:      dfe-engine
#  File:         tests/secrets/test_secrets_seam.py
#  Purpose:      The scalo.secrets seam - real file backend, no mocks
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The minted-secret seam against a real scalo.secrets file backend (no mocks)."""

from __future__ import annotations

import tempfile

import pytest

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


def test_missing_secret_raises():
    from scalo.secrets.exceptions import SecretNotFoundError

    sec = build_secrets(SecretsSettings(provider="file", path=tempfile.mkdtemp()))
    with pytest.raises(SecretNotFoundError):
        sec.get("does/not/exist")
