#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_worker_tagging.py
#  Purpose:      Pure test of the per-query CH settings that attribute hunt cost
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Prove ``query_settings`` builds the exact per-query tags the worker attaches.

``query_settings`` is pure (str -> dict), so it is tested here with no ClickHouse.
``log_comment`` names the hunt so system.query_log can attribute cost back, and is
ALWAYS set. ``workload`` is only added when a workload name is configured - an
undefined CH workload errors on the server, so it must be opt-in (see the worker
docstring). Real query_log attribution is verified in the live-CH Phase A test.
"""

from __future__ import annotations

from dfe_engine.hunt_runner.worker import query_settings


def test_query_settings_always_tags_log_comment():
    # No workload configured -> log_comment only (safe on any CH).
    assert query_settings("brute-force") == {"log_comment": "hunt:brute-force"}


def test_query_settings_embeds_the_exact_hunt_id():
    for hunt_id in ["x", "win_logon_anomaly", "a-very-long-hunt-identifier-42"]:
        assert query_settings(hunt_id)["log_comment"] == f"hunt:{hunt_id}"


def test_query_settings_adds_workload_only_when_configured():
    # default: no workload key at all (never set an undefined workload)
    assert "workload" not in query_settings("h")
    # configured: the workload class is passed through
    assert query_settings("h", workload="hunts")["workload"] == "hunts"


def test_query_settings_values_are_all_strings():
    # CH per-query settings are string key/values on the wire.
    for value in query_settings("h", workload="hunts").values():
        assert isinstance(value, str)
