#  Project:      dfe-engine
#  File:         tests/unit/test_gitops/test_support_drift.py
#  Purpose:      Tests for the SUPPORT-DRIFT pins.yaml override notice
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""SUPPORT-DRIFT notice over a real pins.yaml on disk (no mocks).

The reading of pins.yaml itself is tested in test_pins.py; this is the notice.
"""

from __future__ import annotations

from dfe_engine.gitops.support_drift import log_support_drift


def test_log_support_drift_returns_overrides(tmp_path):
    (tmp_path / "pins.yaml").write_text('overrides:\n  apps:\n    dfe-ui: "v1.1.0@sha256:123"\n')
    assert log_support_drift(tmp_path) == {"dfe-ui": "v1.1.0@sha256:123"}


def test_log_support_drift_quiet_when_clean(tmp_path):
    assert log_support_drift(tmp_path) == {}
