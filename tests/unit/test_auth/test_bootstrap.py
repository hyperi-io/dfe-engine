#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_bootstrap.py
#  Purpose:      First-boot admin seed - posture-aware password (F-ADMIN-CHANGEME)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""bootstrap_auth() first-boot admin password policy.

A non-dev posture with no DFE_ADMIN_PASSWORD must NOT seed the well-known
'changeme' default (CWE-1392); it generates a random one-time password and logs
it exactly once. Dev posture keeps the convenience default, and an
operator-supplied password is honoured verbatim.
"""

from __future__ import annotations

from pathlib import Path

import dfe_engine.auth.bootstrap as bootstrap
from dfe_engine.auth.bootstrap import (
    _DEFAULT_PASSWORD,
    _resolve_admin_password,
    bootstrap_auth,
)


class _RecordingLogger:
    """Stand-in for scalo.logger that records warning() calls (args, kwargs)."""

    def __init__(self) -> None:
        self.warnings: list[tuple] = []

    def warning(self, *args, **kwargs) -> None:
        self.warnings.append((args, kwargs))

    def info(self, *args, **kwargs) -> None:
        pass

    def debug(self, *args, **kwargs) -> None:
        pass

    def error(self, *args, **kwargs) -> None:
        pass

    def exception(self, *args, **kwargs) -> None:
        pass


class TestResolveAdminPassword:
    def test_supplied_password_used_verbatim(self):
        assert _resolve_admin_password("s3cr3t", dev_posture=False) == ("s3cr3t", False)
        assert _resolve_admin_password("s3cr3t", dev_posture=True) == ("s3cr3t", False)

    def test_unset_dev_posture_uses_default(self):
        assert _resolve_admin_password(None, dev_posture=True) == (_DEFAULT_PASSWORD, False)

    def test_unset_non_dev_posture_generates_random(self):
        pw, generated = _resolve_admin_password(None, dev_posture=False)
        assert generated is True
        assert pw != _DEFAULT_PASSWORD
        assert len(pw) >= 24
        # Fresh entropy on every call.
        other, _ = _resolve_admin_password(None, dev_posture=False)
        assert pw != other


class TestBootstrapAdminSeed:
    def test_non_dev_unset_generates_and_logs_once(self, tmp_path: Path, monkeypatch):
        rec = _RecordingLogger()
        monkeypatch.setattr(bootstrap, "logger", rec)
        auth_dir = tmp_path / "auth"

        account_store, _group_store, _api_keys, _role_store, _role_config = bootstrap_auth(
            auth_dir, default_admin_password=None, dev_posture=False
        )

        # Admin exists, but the shipped 'changeme' default must NOT work.
        assert account_store.get("admin") is not None
        assert account_store.verify_password("admin", "changeme") is False

        # Exactly one warning, carrying the real generated password + a rotate note.
        assert len(rec.warnings) == 1
        args, _kwargs = rec.warnings[0]
        message, generated_pw = args[0], args[1]
        assert "IMMEDIATELY" in message
        assert generated_pw != "changeme"
        assert account_store.verify_password("admin", generated_pw) is True

    def test_dev_unset_keeps_convenience_default(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(bootstrap, "logger", _RecordingLogger())
        auth_dir = tmp_path / "auth"

        account_store, *_ = bootstrap_auth(auth_dir, default_admin_password=None, dev_posture=True)
        assert account_store.verify_password("admin", _DEFAULT_PASSWORD) is True

    def test_supplied_password_used_as_is(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(bootstrap, "logger", _RecordingLogger())
        auth_dir = tmp_path / "auth"

        account_store, *_ = bootstrap_auth(
            auth_dir, default_admin_password="operator-set-pw", dev_posture=False
        )
        assert account_store.verify_password("admin", "operator-set-pw") is True
        assert account_store.verify_password("admin", "changeme") is False
