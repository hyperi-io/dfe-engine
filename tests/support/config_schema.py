#  Project:      dfe-engine
#  File:         tests/support/config_schema.py
#  Purpose:      Hold a config the engine writes against the JSON Schema an app ships
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Walk a config the engine writes against an app's ``docs/config-schema.json``.

Each Rust app derives that schema from its config structs, so a key it has no field
for is one serde drops without a word, and a value of the wrong JSON type is one it
refuses at load. ``unread_keys`` finds the first, ``type_mismatches`` the second.
"""

from typing import Any

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "boolean": (bool,),
    "integer": (int,),
    "number": (int, float),
    "null": (type(None),),
}


def definitions(schema: dict[str, Any]) -> dict[str, Any]:
    """The named sub-schemas ``$ref`` points into."""
    return schema.get("$defs", schema.get("definitions", {}))


def variants(node: Any, defs: dict[str, Any]) -> list[dict[str, Any]]:
    """The schemas a value at ``node`` may match, references and unions expanded."""
    if not isinstance(node, dict):
        return []
    while "$ref" in node:
        node = defs[node["$ref"].rsplit("/", 1)[-1]]
    found: list[dict[str, Any]] = []
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in node.get(key, []):
            found.extend(variants(sub, defs))
    return found or [node]


def _fields(structs: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The named fields of ``structs``, and the schema any other key takes, if one does."""
    fields: dict[str, Any] = {}
    entries: dict[str, Any] | None = None
    for struct in structs:
        fields.update(struct.get("properties", {}))
        if isinstance(struct.get("additionalProperties"), dict):
            entries = struct["additionalProperties"]
    return fields, entries


def unread_keys(value: Any, node: Any, defs: dict[str, Any], path: str = "") -> list[str]:
    """Every key path in ``value`` that the schema ``node`` gives no field for."""
    found = variants(node, defs)
    if isinstance(value, list):
        items = [v["items"] for v in found if isinstance(v.get("items"), dict)]
        listed: list[str] = []
        if items:
            for item in value:
                listed.extend(unread_keys(item, items[0], defs, f"{path}[]"))
        return listed
    if not isinstance(value, dict):
        return []
    structs = [v for v in found if "properties" in v or "additionalProperties" in v]
    # No struct to compare against, or one that takes any key: nothing is unread.
    if not structs or any(v.get("additionalProperties") is True for v in structs):
        return []
    fields, entries = _fields(structs)
    unread: list[str] = []
    for key, sub in value.items():
        where = f"{path}.{key}" if path else key
        if key in fields:
            unread.extend(unread_keys(sub, fields[key], defs, where))
        elif entries is not None:
            unread.extend(unread_keys(sub, entries, defs, where))
        else:
            unread.append(where)
    return unread


def _is(value: Any, json_type: str) -> bool:
    """Whether ``value`` is of the JSON type the schema names, where a bool is never a number."""
    if isinstance(value, bool) and json_type in ("integer", "number"):
        return False
    return isinstance(value, _JSON_TYPES.get(json_type, ()))


def type_mismatches(value: Any, node: Any, defs: dict[str, Any], path: str = "") -> list[str]:
    """Every path in ``value`` whose JSON type no variant of ``node`` takes.

    A variant naming no type takes any value. A key the schema has no field for is
    ``unread_keys``'s to report, so it is not descended into here.
    """
    found = variants(node, defs)
    allowed: set[str] = set()
    for variant in found:
        named = variant.get("type")
        if named is None:
            return []
        allowed.update([named] if isinstance(named, str) else named)
    if not any(_is(value, json_type) for json_type in allowed):
        return [f"{path or '<root>'}: {type(value).__name__}, the schema takes {sorted(allowed)}"]
    wrong: list[str] = []
    if isinstance(value, dict):
        fields, entries = _fields(found)
        for key, sub in value.items():
            schema = fields.get(key, entries)
            if schema is not None:
                wrong.extend(type_mismatches(sub, schema, defs, f"{path}.{key}" if path else key))
    elif isinstance(value, list):
        items = [v["items"] for v in found if isinstance(v.get("items"), dict)]
        if items:
            for item in value:
                wrong.extend(type_mismatches(item, items[0], defs, f"{path}[]"))
    return wrong
