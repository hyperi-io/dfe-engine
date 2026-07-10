#  Project:      dfe-engine
#  File:         cli/auto/output.py
#  Purpose:      Output-format registry (json/yaml/table/value/text) + --query
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Format the API response for the terminal, mirroring aws-cli's formatter set.

``FORMATS`` maps a name to a ``callable(data, stream)``. ``get_formatter(name)``
returns one. ``--query`` (JMESPath) is applied BEFORE formatting. jmespath is a
hard dependency, so ``--query`` always resolves via jmespath.

Default format is chosen by the caller (see build.py): ``describe`` -> yaml,
``list`` -> table, everything else -> json; and json whenever stdout is not a tty
(script-safe).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import jmespath

from .vendor import table as _table
from .vendor import text as _text

Formatter = Callable[[Any, Any], None]


def _default_json(obj: Any) -> Any:
    """Best-effort JSON encoder for stray non-serialisable values."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


def format_json(data: Any, stream: Any) -> None:
    json.dump(data, stream, indent=2, default=_default_json, sort_keys=False)
    stream.write("\n")


def format_yaml(data: Any, stream: Any) -> None:
    # Prefer the project's ruamel wrapper (YAML 1.2) for consistency with the API.
    try:
        from dfe_engine.yaml_utils import yaml_dump_string

        stream.write(yaml_dump_string(_plain(data)))
    except Exception:
        from ruamel.yaml import YAML

        yaml = YAML()
        yaml.default_flow_style = False
        yaml.dump(_plain(data), stream)


def _rows_for_table(data: Any) -> list[dict[str, Any]]:
    """Coerce the payload into a list-of-dicts for tabular rendering."""
    if isinstance(data, dict):
        # A paginated envelope or a single object.
        if isinstance(data.get("items"), list):
            data = data["items"]
        else:
            data = [data]
    if not isinstance(data, list):
        return [{"value": data}]
    rows = []
    for el in data:
        if isinstance(el, dict):
            rows.append(el)
        else:
            rows.append({"value": el})
    return rows


def _scalar_columns(rows: list[dict[str, Any]]) -> list[str]:
    """Union of keys whose values are scalar in at least one row (order-stable)."""
    cols: list[str] = []
    for row in rows:
        for key, value in row.items():
            if not isinstance(value, (dict, list)) and key not in cols:
                cols.append(key)
    return cols


def format_table(data: Any, stream: Any) -> None:
    rows = _rows_for_table(data)
    if not rows:
        stream.write("\n")
        return
    columns = _scalar_columns(rows)
    if not columns:
        # Nothing scalar to tabulate - fall back to json so we never print blank.
        format_json(data, stream)
        return
    multitable = _table.MultiTable()
    multitable.add_row_header(columns)
    for row in rows:
        multitable.add_row([_cell(row.get(col, "")) for col in columns])
    multitable.render(stream)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=_default_json)
    return str(value)


def _value_scalar_keys(rows: list[Any]) -> list[str]:
    """Order-stable union of keys whose value is scalar in at least one dict row."""
    keys: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            for key, value in row.items():
                if not isinstance(value, (dict, list)) and key not in keys:
                    keys.append(key)
    return keys


def _write_value_row(row: dict[str, Any], stream: Any, keys: list[str] | None) -> None:
    """Write one tab-joined row of scalar values (no keys)."""
    if keys is None:
        vals = [_cell(v) for v in row.values() if not isinstance(v, (dict, list))]
    else:
        vals = [_cell(row.get(k, "")) for k in keys]
    stream.write("\t".join(vals))
    stream.write("\n")


def format_value(data: Any, stream: Any) -> None:
    # `value` = scalar leaf values only, WITHOUT keys (aws-cli's `text` minus keys).
    # A list of dicts -> one tab-joined row of scalar values per item; a list of
    # scalars -> one per line; a single dict -> its scalar values tab-joined. Nested
    # dict/list values are dropped (only the scalar leaves are printed).
    plain = _plain(data)
    if isinstance(plain, dict):
        _write_value_row(plain, stream, None)
        return
    if isinstance(plain, list):
        if any(isinstance(el, dict) for el in plain):
            keys = _value_scalar_keys(plain)
            for el in plain:
                if isinstance(el, dict):
                    _write_value_row(el, stream, keys)
                elif not isinstance(el, list):
                    stream.write(_cell(el))
                    stream.write("\n")
            return
        for el in plain:
            if not isinstance(el, (dict, list)):
                stream.write(_cell(el))
                stream.write("\n")
        return
    stream.write(_cell(plain))
    stream.write("\n")


def format_text(data: Any, stream: Any) -> None:
    _text.format_text(_plain(data), stream)


def _plain(data: Any) -> Any:
    """Round-trip through json to drop pydantic/ruamel wrappers -> plain types."""
    return json.loads(json.dumps(data, default=_default_json))


FORMATS: dict[str, Formatter] = {
    "json": format_json,
    "yaml": format_yaml,
    "table": format_table,
    "value": format_value,
    "text": format_text,
}


def get_formatter(name: str) -> Formatter:
    """Return the formatter for ``name`` (KeyError-safe: falls back to json)."""
    return FORMATS.get(name, format_json)


def apply_query(data: Any, expression: str | None) -> Any:
    """Apply a JMESPath ``--query`` before formatting (jmespath is a hard dep)."""
    if not expression:
        return data
    return jmespath.search(expression, _plain(data))
