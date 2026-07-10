#  Project:      dfe-engine
#  File:         tests/unit/test_cli_auto/test_format_units.py
#  Purpose:      Output formatters + default-format selection (output.py, build.py)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Pure-unit coverage of the formatter registry + the default-format chooser.

``test_output_query`` exercises json/table/value/text/--query end to end; this
fills the branches it does not reach: the ``yaml`` formatter round-trip, the
non-serialisable JSON encoder (datetime / ``model_dump``), the table's
no-scalar-column fall-back to json and its bool/None cell rendering, and the
``_default_format`` matrix (yaml for describe, table for list, else json, and json
whenever stdout is not a tty).
"""

from __future__ import annotations

import datetime
import io
import json

from dfe_engine.cli.auto import build, output
from dfe_engine.cli.auto.build import GlobalOptions, _default_format
from dfe_engine.yaml_utils import yaml_load_string


def _render(fmt: str, data) -> str:
    buf = io.StringIO()
    output.get_formatter(fmt)(data, buf)
    return buf.getvalue()


# --- registry ----------------------------------------------------------------


def test_get_formatter_unknown_falls_back_to_json():
    assert output.get_formatter("nope") is output.format_json
    assert output.get_formatter("yaml") is output.format_yaml


# --- yaml + json encoders ----------------------------------------------------


def test_format_yaml_round_trips():
    data = {"name": "alpha", "count": 3, "nested": {"k": "v"}}
    loaded = yaml_load_string(_render("yaml", data))
    assert loaded == data


def test_format_json_encodes_datetime_and_model_dump():
    class Model:
        def model_dump(self):
            return {"x": 1}

    out = _render("json", {"when": datetime.datetime(2026, 7, 10, 12, 0, 0), "m": Model()})
    assert "2026-07-10T12:00:00" in out
    assert '"x": 1' in out


# --- table -------------------------------------------------------------------


def test_format_table_no_scalar_columns_falls_back_to_json():
    # Every value is nested -> nothing to tabulate -> json (never a blank table).
    data = [{"a": {"nested": 1}}]
    out = _render("table", data)
    assert json.loads(out) == data


def test_format_table_renders_bool_and_none_cells():
    out = _render("table", [{"name": "x", "ok": True, "bad": None, "meta": {"k": 1}}])
    for column in ("name", "ok", "bad"):
        assert column in out
    assert "true" in out  # bool -> lower-case token
    # The nested `meta` column is dropped (not scalar), never json-jammed into a cell.
    assert "meta" not in out


# --- value on a bare scalar --------------------------------------------------


def test_format_value_bare_scalar():
    assert _render("value", 5) == "5\n"


# --- --query passthrough -----------------------------------------------------


def test_apply_query_none_is_passthrough():
    data = [{"name": "a"}]
    assert output.apply_query(data, None) == data


def test_apply_query_projects():
    data = [{"name": "a"}, {"name": "b"}]
    assert output.apply_query(data, "[].name") == ["a", "b"]


# --- default-format selection (build._default_format) ------------------------


class _FakeStdout:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def write(self, *_a) -> None:  # pragma: no cover - never written to here
        pass

    def flush(self) -> None:  # pragma: no cover
        pass


def test_default_format_tty_selection(monkeypatch):
    monkeypatch.setattr(build.sys, "stdout", _FakeStdout(True))
    opts = GlobalOptions()
    assert _default_format(opts, "describe") == "yaml"
    assert _default_format(opts, "list") == "table"
    assert _default_format(opts, "create") == "json"


def test_default_format_non_tty_is_always_json(monkeypatch):
    monkeypatch.setattr(build.sys, "stdout", _FakeStdout(False))
    opts = GlobalOptions()
    assert _default_format(opts, "describe") == "json"
    assert _default_format(opts, "list") == "json"


def test_default_format_explicit_flag_wins(monkeypatch):
    monkeypatch.setattr(build.sys, "stdout", _FakeStdout(True))
    opts = GlobalOptions(format="value")
    # An explicit --format beats the verb/tty heuristic.
    assert _default_format(opts, "list") == "value"
