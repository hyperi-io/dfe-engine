#  Project:      dfe-engine
#  File:         tests/unit/test_sigma/test_security_hardening.py
#  Purpose:      for-opus security hardening: CAST breakout + file:// provider URL
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Sigma is now a data_analyst (not admin-only) surface, so its operator-declared
inputs must be hardened. These pin the CAST-type breakout rejection and the
file:// provider-URL rejection."""

from __future__ import annotations

import pytest

from dfe_engine.sigma.providers import ProviderConfig, ProviderKind
from dfe_engine.sigma.views import SigmaViewError, _safe_type


class TestCastTypeHardening:
    def test_legit_types_pass(self):
        # Real parametrised types with balanced parens still pass. (The '=' in an
        # Enum literal is outside the charset - pre-existing, not my change.)
        assert _safe_type("String") == "String"
        assert _safe_type("UInt32") == "UInt32"
        assert _safe_type("Nullable(String)") == "Nullable(String)"
        assert _safe_type("DateTime64(3)") == "DateTime64(3)"
        assert _safe_type("Array(String)") == "Array(String)"

    def test_premature_close_paren_breakout_rejected(self):
        # 'String) OR (1=1' has an EQUAL ()-count but closes the CAST early - the
        # running-depth check catches the negative dip a bare count would miss.
        with pytest.raises(SigmaViewError):
            _safe_type("String) OR (1=1")

    def test_unbalanced_parens_rejected(self):
        with pytest.raises(SigmaViewError):
            _safe_type("Decimal(10, 2")


class TestProviderUrlHardening:
    def test_file_url_rejected(self):
        with pytest.raises(ValueError, match="file://"):
            ProviderConfig(
                name="evil", kind=ProviderKind.GIT_REPO, options={"url": "file:///etc/passwd"}
            )

    def test_https_url_accepted(self):
        cfg = ProviderConfig(
            name="ok",
            kind=ProviderKind.GIT_REPO,
            options={"url": "https://github.com/SigmaHQ/sigma"},
        )
        assert cfg.options["url"].startswith("https://")


class TestProviderReachConfinement:
    """S1: register/update confine a provider's file/host reach."""

    def _request(self, tmp_path):
        from types import SimpleNamespace

        return SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(settings=SimpleNamespace(config_dir=str(tmp_path)))
            )
        )

    def test_local_files_dir_escape_rejected(self, tmp_path):
        import pytest
        from fastapi import HTTPException

        from dfe_engine.api.v1.sigma import _validate_provider_reach

        cfg = ProviderConfig(
            name="evil", kind=ProviderKind.LOCAL_FILES, options={"directory": "../../etc"}
        )
        with pytest.raises(HTTPException) as exc:
            _validate_provider_reach(self._request(tmp_path), cfg)
        assert exc.value.status_code == 422

    def test_local_files_dir_under_config_ok(self, tmp_path):
        from dfe_engine.api.v1.sigma import _validate_provider_reach

        cfg = ProviderConfig(
            name="ok", kind=ProviderKind.LOCAL_FILES, options={"directory": "sigma/rules"}
        )
        _validate_provider_reach(self._request(tmp_path), cfg)  # no raise

    def test_git_host_allowlist_rejects_off_list(self, tmp_path, monkeypatch):
        import pytest
        from fastapi import HTTPException

        from dfe_engine.api.v1.sigma import _validate_provider_reach

        monkeypatch.setenv("DFE_SIGMA_ALLOWED_HOSTS", "github.com,gitlab.com")
        cfg = ProviderConfig(
            name="evil", kind=ProviderKind.GIT_REPO, options={"url": "https://evil.example/x"}
        )
        with pytest.raises(HTTPException) as exc:
            _validate_provider_reach(self._request(tmp_path), cfg)
        assert exc.value.status_code == 422

    def test_git_host_on_allowlist_ok(self, tmp_path, monkeypatch):
        from dfe_engine.api.v1.sigma import _validate_provider_reach

        monkeypatch.setenv("DFE_SIGMA_ALLOWED_HOSTS", "github.com")
        cfg = ProviderConfig(
            name="ok",
            kind=ProviderKind.GIT_REPO,
            options={"url": "https://github.com/SigmaHQ/sigma"},
        )
        _validate_provider_reach(self._request(tmp_path), cfg)  # no raise
