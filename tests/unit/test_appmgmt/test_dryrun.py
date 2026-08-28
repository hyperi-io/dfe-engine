#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_dryrun.py
#  Purpose:      Tests for running an authored transform over sampled events
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The dry-run seam: bounds, honest statuses, and no invented results.

The backend is absent in this checkout, so the tests that need one substitute a
fake at the import site. That is the point of the seam - an absent backend must
report ``unavailable``, never a pass.
"""

from __future__ import annotations

import sys
import types

import pytest

from dfe_engine.appmgmt import dryrun

EVENTS = ['{"a": 1}', '{"a": 2}', '{"a": 3}']


@pytest.fixture
def fake_backend(monkeypatch):
    """Install a stand-in vectordotdev whose quick_vrl_test the test controls."""

    def install(fn):
        module = types.ModuleType("vectordotdev.native_vector_executor")
        module.quick_vrl_test = fn
        package = types.ModuleType("vectordotdev")
        monkeypatch.setitem(sys.modules, "vectordotdev", package)
        monkeypatch.setitem(sys.modules, "vectordotdev.native_vector_executor", module)

    return install


class TestSwitch:
    def test_disabled_says_so_rather_than_passing(self):
        result = dryrun.run_language("vrl", ".a = 1", EVENTS, enabled=False)
        assert result.status is dryrun.DryRunStatus.DISABLED
        assert result.events == ()

    def test_no_events_completes_without_a_backend(self):
        result = dryrun.run_language("vrl", ".a = 1", [], enabled=True)
        assert result.status is dryrun.DryRunStatus.COMPLETED
        assert result.events == ()

    def test_an_absent_backend_is_unavailable_not_valid(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "vectordotdev", None)
        result = dryrun.run_language("vrl", ".a = 1", EVENTS, enabled=True)
        assert result.status is dryrun.DryRunStatus.UNAVAILABLE

    def test_an_unknown_language_is_unsupported(self):
        result = dryrun.run_language("cobol", "IDENTIFICATION", EVENTS, enabled=True)
        assert result.status is dryrun.DryRunStatus.UNSUPPORTED


class TestOutcomes:
    def test_each_event_reports_before_and_after(self, fake_backend):
        fake_backend(lambda program, events, max_events: [f"{e}!" for e in events])
        result = dryrun.run_language("vrl", ".a = 1", EVENTS, enabled=True)
        assert result.status is dryrun.DryRunStatus.COMPLETED
        assert [e.before for e in result.events] == EVENTS
        assert [e.after for e in result.events] == [f"{e}!" for e in EVENTS]
        assert result.succeeded == 3

    def test_a_per_event_error_is_counted_not_raised(self, fake_backend):
        fake_backend(
            lambda program, events, max_events: [
                {"output": events[0]},
                {"error": "field .b is undefined"},
                {"output": events[2]},
            ]
        )
        result = dryrun.run_language("vrl", ".b", EVENTS, enabled=True)
        assert (result.succeeded, result.failed) == (2, 1)
        assert result.events[1].error == "field .b is undefined"

    def test_a_dropped_event_is_neither_a_success_nor_an_error(self, fake_backend):
        fake_backend(lambda program, events, max_events: [events[0], None, events[2]])
        result = dryrun.run_language("vrl", "abort", EVENTS, enabled=True)
        assert (result.succeeded, result.failed, result.dropped) == (2, 0, 1)

    def test_unchanged_output_is_reported_as_unchanged(self, fake_backend):
        fake_backend(lambda program, events, max_events: list(events))
        result = dryrun.run_language("vrl", ".", EVENTS, enabled=True)
        assert [e.changed for e in result.events] == [False, False, False]

    def test_a_compile_error_fails_the_run_not_an_event(self, fake_backend):
        fake_backend(lambda program, events, max_events: {"ok": False, "error": "syntax error"})
        result = dryrun.run_language("vrl", "!!!", EVENTS, enabled=True)
        assert result.status is dryrun.DryRunStatus.FAILED
        assert "syntax error" in result.message

    def test_an_unrecognised_result_is_unavailable_not_guessed(self, fake_backend):
        fake_backend(lambda program, events, max_events: 42)
        result = dryrun.run_language("vrl", ".", EVENTS, enabled=True)
        assert result.status is dryrun.DryRunStatus.UNAVAILABLE

    def test_a_backend_that_raises_does_not_propagate(self, fake_backend):
        def boom(program, events, max_events):
            raise RuntimeError("linked against nothing")

        fake_backend(boom)
        result = dryrun.run_language("vrl", ".", EVENTS, enabled=True)
        assert result.status is dryrun.DryRunStatus.UNAVAILABLE


class TestBounds:
    def test_events_are_capped_and_the_cap_is_reported(self, fake_backend):
        seen = {}

        def record(program, events, max_events):
            seen["count"] = len(events)
            return list(events)

        fake_backend(record)
        many = [f'{{"i": {i}}}' for i in range(dryrun.MAX_EVENTS + 25)]
        result = dryrun.run_language("vrl", ".", many, enabled=True)
        assert seen["count"] == dryrun.MAX_EVENTS
        assert result.truncated is True

    def test_output_over_the_size_ceiling_is_cut(self, fake_backend):
        big = "x" * 40_000
        fake_backend(lambda program, events, max_events: [big for _ in events])
        result = dryrun.run_language("vrl", ".", [big] * 10, enabled=True)
        assert result.truncated is True
        assert len(result.events) < 10


class TestVectorYaml:
    def test_a_remap_transform_runs_its_inline_vrl(self, fake_backend):
        captured = {}

        def record(program, events, max_events):
            captured["program"] = program
            return list(events)

        fake_backend(record)
        content = "type: remap\nsource: |\n  .a = 1\n"
        result = dryrun.run_language("yaml", content, EVENTS, enabled=True)
        assert result.status is dryrun.DryRunStatus.COMPLETED
        assert ".a = 1" in captured["program"]

    def test_a_non_remap_transform_says_why_it_cannot_run(self):
        content = "type: filter\ncondition: .a > 1\n"
        result = dryrun.run_language("yaml", content, EVENTS, enabled=True)
        assert result.status is dryrun.DryRunStatus.UNSUPPORTED
        assert "filter" in result.message

    def test_a_remap_with_no_inline_source_is_unsupported(self):
        result = dryrun.run_language("yaml", "type: remap\nfile: /x.vrl\n", EVENTS, enabled=True)
        assert result.status is dryrun.DryRunStatus.UNSUPPORTED

    def test_invalid_yaml_is_unsupported_not_a_crash(self):
        result = dryrun.run_language("yaml", "type: [unclosed\n", EVENTS, enabled=True)
        assert result.status is dryrun.DryRunStatus.UNSUPPORTED
