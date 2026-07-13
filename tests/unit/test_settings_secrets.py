#  Project:      dfe-engine
#  File:         tests/unit/test_settings_secrets.py
#  Purpose:      SecretsSettings - the scalo.secrets backend-by-config seam
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The secrets seam is chosen by config, never hardcoded (see docs/deployment/backing-services.md)."""

from __future__ import annotations

from dfe_engine.settings import load_settings, reset_settings


def test_secrets_defaults_to_file(monkeypatch):
    for k in ("DFE_SECRETS_PROVIDER", "DFE_SECRETS_PATH", "DFE_SECRETS_ADDR"):
        monkeypatch.delenv(k, raising=False)
    reset_settings()
    s = load_settings()
    assert s.secrets.provider == "file"
    reset_settings()


def test_secrets_provider_from_env(monkeypatch):
    monkeypatch.setenv("DFE_SECRETS_PROVIDER", "openbao")
    monkeypatch.setenv("DFE_SECRETS_ADDR", "https://bao:8200")
    reset_settings()
    s = load_settings()
    assert s.secrets.provider == "openbao"
    assert s.secrets.addr == "https://bao:8200"
    reset_settings()
