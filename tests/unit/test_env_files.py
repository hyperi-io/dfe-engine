#  Project:      dfe-engine
#  File:         tests/unit/test_env_files.py
#  Purpose:      Tests for .env loading at startup
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import os

from dfe_engine.env_files import load_env_files


def test_load_env_files_respects_existing_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('FROM_DOTENV="from-file"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FROM_DOTENV", "from-shell")
    monkeypatch.delenv("DFE_ENV_FILE", raising=False)
    monkeypatch.delenv("DFE_CONFIG_DIR", raising=False)

    load_env_files()

    assert os.environ["FROM_DOTENV"] == "from-shell"


def test_load_env_files_fills_missing_vars(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('OKTA_TEST_CLIENT_ID="0oa-test"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OKTA_TEST_CLIENT_ID", raising=False)
    monkeypatch.delenv("DFE_ENV_FILE", raising=False)
    monkeypatch.delenv("DFE_CONFIG_DIR", raising=False)

    load_env_files()

    assert os.environ.get("OKTA_TEST_CLIENT_ID") == "0oa-test"
