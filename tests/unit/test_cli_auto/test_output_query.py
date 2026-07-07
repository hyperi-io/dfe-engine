"""Output formats + --query filtering."""

from __future__ import annotations

import io
import json

from dfe_engine.cli.auto import output


def _render(fmt, data) -> str:
    buf = io.StringIO()
    output.get_formatter(fmt)(data, buf)
    return buf.getvalue()


def test_format_value_differs_from_text_and_drops_keys():
    # `value` prints scalar leaf values only (no keys); `text` carries keys as
    # identifiers for nested structures. They must not be byte-identical.
    data = [
        {"name": "alpha", "count": 1, "nested": {"k": "v"}},
        {"name": "bravo", "count": 2, "nested": {"k": "w"}},
    ]
    value_out = _render("value", data)
    text_out = _render("text", data)
    assert value_out != text_out
    # value: one tab-joined scalar row per item, no keys, nested dropped.
    assert value_out == "alpha\t1\nbravo\t2\n"
    assert "nested" not in value_out
    assert "k" not in value_out


def test_format_value_list_of_scalars_one_per_line():
    assert _render("value", ["a", "b", "c"]) == "a\nb\nc\n"


def test_format_value_single_dict_scalars_tab_joined():
    out = _render("value", {"name": "alpha", "count": 3, "nested": {"x": 1}})
    assert out == "alpha\t3\n"


def _seed_orgs(harness) -> None:
    harness.seed_admin()
    for name in ("alpha", "bravo"):
        assert harness.invoke(["orgs", "create", "--name", name]).exit_code == 0


def test_format_json_is_parseable(harness):
    _seed_orgs(harness)
    result = harness.invoke(["--format", "json", "orgs", "list"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert {row["name"] for row in data} >= {"alpha", "bravo"}


def test_format_table_renders_columns(harness):
    _seed_orgs(harness)
    result = harness.invoke(["--format", "table", "orgs", "list"])
    assert result.exit_code == 0, result.output
    # ASCII table: a header cell for `name` and the row values.
    assert "name" in result.output
    assert "alpha" in result.output
    assert "|" in result.output


def test_query_selects_names(harness):
    _seed_orgs(harness)
    result = harness.invoke(["--query", "[].name", "--format", "json", "orgs", "list"])
    assert result.exit_code == 0, result.output
    names = json.loads(result.output)
    assert isinstance(names, list)
    assert "alpha" in names
    assert "bravo" in names
    # Projected to bare strings, not objects.
    assert all(isinstance(n, str) for n in names)


def test_default_format_json_off_a_pipe(harness):
    # CliRunner stdout is not a tty, so the default format is json (script-safe).
    _seed_orgs(harness)
    result = harness.invoke(["orgs", "list"])
    assert result.exit_code == 0, result.output
    json.loads(result.output)  # parses -> default was json
