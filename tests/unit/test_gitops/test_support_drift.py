#  Project:      dfe-engine
#  File:         tests/unit/test_gitops/test_support_drift.py
#  Purpose:      Tests for the SUPPORT-DRIFT pins.yaml override notice
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""SUPPORT-DRIFT notice over a real pins.yaml on disk (no mocks)."""

from __future__ import annotations

from dfe_engine.gitops.support_drift import log_support_drift, support_drift_overrides


def _write_pins(tmp_path, text: str) -> None:
    (tmp_path / "pins.yaml").write_text(text)


def test_no_pins_file_is_empty(tmp_path):
    assert support_drift_overrides(tmp_path) == {}


def test_pins_without_overrides_is_empty(tmp_path):
    _write_pins(tmp_path, 'base:\n  dfe-infra: "2.2.0"\nchannel: "stable"\n')
    assert support_drift_overrides(tmp_path) == {}


def test_overrides_flattened_across_groups(tmp_path):
    _write_pins(
        tmp_path,
        "base:\n"
        '  dfe-infra: "2.2.0"\n'
        "overrides:\n"
        "  apps:\n"
        '    dfe-receiver: "v1.16.0@sha256:abc"\n'
        '    dfe-loader: "v1.19.0@sha256:def"\n',
    )
    overrides = support_drift_overrides(tmp_path)
    assert overrides == {
        "dfe-receiver": "v1.16.0@sha256:abc",
        "dfe-loader": "v1.19.0@sha256:def",
    }


def test_malformed_pins_never_raises(tmp_path):
    _write_pins(tmp_path, "{{{ not yaml")
    assert support_drift_overrides(tmp_path) == {}


def test_log_support_drift_returns_overrides(tmp_path):
    _write_pins(
        tmp_path,
        'overrides:\n  apps:\n    dfe-ui: "v1.1.0@sha256:123"\n',
    )
    assert log_support_drift(tmp_path) == {"dfe-ui": "v1.1.0@sha256:123"}


def test_log_support_drift_quiet_when_clean(tmp_path):
    assert log_support_drift(tmp_path) == {}
