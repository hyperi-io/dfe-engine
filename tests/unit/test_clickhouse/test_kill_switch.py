#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_kill_switch.py
#  Purpose:      Kill switch - severity dial + min(caller,cap) clamp + exemptions
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Kill switch: the incident brake clamps user-query ceilings, exempts ops paths."""

from __future__ import annotations

import pytest

import dfe_engine.settings as settings_module
from dfe_engine.clickhouse.kill_switch import apply_caps, clamp_for_profile
from dfe_engine.clickhouse.profiles import Profile
from dfe_engine.settings import (
    ChKillSwitchSettings,
    ChQueryCaps,
    ClickHouseSettings,
    DFESettings,
)

# ---- apply_caps (pure) -------------------------------------------------------


def test_apply_caps_tightens_to_cap():
    out = apply_caps({"max_execution_time": 25}, {"max_execution_time": 5})
    assert out["max_execution_time"] == 5  # min(25, 5)


def test_apply_caps_leaves_stricter_caller_alone():
    out = apply_caps({"max_execution_time": 3}, {"max_execution_time": 5})
    assert out["max_execution_time"] == 3  # caller already below the cap


def test_apply_caps_installs_cap_when_caller_unset():
    out = apply_caps({}, {"max_threads": 2})
    assert out["max_threads"] == 2


def test_apply_caps_empty_is_noop():
    base = {"max_execution_time": 25}
    assert apply_caps(base, {}) == base


def test_apply_caps_non_numeric_caller_clamps_hard():
    out = apply_caps({"max_execution_time": "oops"}, {"max_execution_time": 5})
    assert out["max_execution_time"] == 5


def test_apply_caps_never_mutates_input():
    base = {"max_execution_time": 25}
    apply_caps(base, {"max_execution_time": 5})
    assert base["max_execution_time"] == 25


# ---- ChKillSwitchSettings ----------------------------------------------------


def test_severity_off_is_inactive_and_empty():
    ks = ChKillSwitchSettings(severity="off")
    assert ks.active is False
    assert ks.active_caps() == {}


def test_severity_light_returns_light_caps():
    ks = ChKillSwitchSettings(severity="light")
    assert ks.active is True
    assert ks.active_caps() == {"max_execution_time": 15, "max_threads": 8}


def test_severity_full_returns_full_caps():
    ks = ChKillSwitchSettings(severity="full")
    assert ks.active_caps() == {"max_execution_time": 5, "max_threads": 2}


def test_severity_is_case_insensitive():
    assert ChKillSwitchSettings(severity="FULL").active is True


def test_unknown_severity_treated_as_off():
    ks = ChKillSwitchSettings(severity="bogus")
    assert ks.active is False
    assert ks.active_caps() == {}


def test_query_caps_as_dict_drops_none():
    caps = ChQueryCaps(max_execution_time=5, max_memory_usage=None)
    assert caps.as_dict() == {"max_execution_time": 5}


# ---- clamp_for_profile (settings-backed) -------------------------------------


@pytest.fixture
def brake(monkeypatch):
    """Install a kill-switch severity into the live settings for the test."""

    def _set(severity: str, **caps: int) -> None:
        ks = ChKillSwitchSettings(severity=severity)
        if caps:
            ks = ChKillSwitchSettings(severity=severity, full=ChQueryCaps(**caps))
        monkeypatch.setattr(
            settings_module,
            "_settings",
            DFESettings(clickhouse=ClickHouseSettings(kill_switch=ks)),
        )

    return _set


def test_clamp_user_query_when_full(brake):
    brake("full")
    out = clamp_for_profile({"max_execution_time": 25}, Profile.QUERY)
    assert out["max_execution_time"] == 5


def test_clamp_is_noop_when_off(brake):
    brake("off")
    out = clamp_for_profile({"max_execution_time": 25}, Profile.QUERY)
    assert out["max_execution_time"] == 25


@pytest.mark.parametrize(
    "profile", [Profile.INTERNAL, Profile.MIGRATE, Profile.INSERT, Profile.DELETE, Profile.OPTIMIZE]
)
def test_ops_profiles_are_exempt(brake, profile):
    brake("full")
    out = clamp_for_profile({"max_execution_time": 25}, profile)
    assert out["max_execution_time"] == 25  # exempt -> unclamped


def test_tracing_profile_is_clamped(brake):
    brake("full")
    out = clamp_for_profile({"max_execution_time": 25}, Profile.TRACING)
    assert out["max_execution_time"] == 5  # user-facing read path -> clamped


def test_clamp_preserves_other_settings(brake):
    brake("full", max_execution_time=5)
    out = clamp_for_profile({"max_execution_time": 25, "log_comment": "x"}, Profile.QUERY)
    assert out["log_comment"] == "x"
    assert out["max_execution_time"] == 5


def test_env_severity_maps_into_settings(monkeypatch):
    """DFE_CLICKHOUSE_KILL_SWITCH_SEVERITY flips the gitops dial via env."""
    monkeypatch.setenv("DFE_CLICKHOUSE_KILL_SWITCH_SEVERITY", "full")
    monkeypatch.setattr(settings_module, "_settings", None)
    try:
        assert settings_module.get_settings().clickhouse.kill_switch.severity == "full"
    finally:
        monkeypatch.setattr(settings_module, "_settings", None)
