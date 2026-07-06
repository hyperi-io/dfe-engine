#  Project:      dfe-engine
#  File:         tests/unit/test_env_refs.py
#  Purpose:      resolve_env_ref - config env-var-name -> runtime value
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""resolve_env_ref: name -> value, with the empty-string and None default styles."""

from __future__ import annotations

from dfe_engine.env_refs import resolve_env_ref


def test_resolves_a_set_var(monkeypatch):
    monkeypatch.setenv("DFE_TEST_REF", "secret-value")
    assert resolve_env_ref("DFE_TEST_REF") == "secret-value"


def test_empty_name_returns_default():
    assert resolve_env_ref("") == ""
    assert resolve_env_ref(None) == ""
    assert resolve_env_ref("", default=None) is None


def test_unset_var_returns_default(monkeypatch):
    monkeypatch.delenv("DFE_TEST_MISSING", raising=False)
    assert resolve_env_ref("DFE_TEST_MISSING") == ""
    assert resolve_env_ref("DFE_TEST_MISSING", default=None) is None


def test_none_default_style_for_oidc(monkeypatch):
    # The OIDC adapters treat "unconfigured" and "unset" alike as a hard None.
    monkeypatch.delenv("DFE_TEST_OIDC", raising=False)
    assert resolve_env_ref(None, default=None) is None
    assert resolve_env_ref("DFE_TEST_OIDC", default=None) is None
    monkeypatch.setenv("DFE_TEST_OIDC", "cid")
    assert resolve_env_ref("DFE_TEST_OIDC", default=None) == "cid"
