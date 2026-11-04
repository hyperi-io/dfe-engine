"""The engine's list helpers beside scalo's Page: search and the folder tree."""

import pytest
from pydantic import BaseModel

from dfe_engine.api.conventions import PathTree, apply_search

ROWS = [
    {"name": "Windows Audit", "desc": "Windows security events"},
    {"name": "Linux Syslog", "desc": "Linux system logs"},
    {"name": "windows_audit_2024", "desc": ""},
]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("linux", ["Linux Syslog"]),
        ("WINDOWS", ["Windows Audit", "windows_audit_2024"]),
        ("audit", ["Windows Audit", "windows_audit_2024"]),
        ("zzz", []),
        (None, [row["name"] for row in ROWS]),
        ("", [row["name"] for row in ROWS]),
    ],
)
def test_search_matches_any_field_ignoring_case(query, expected):
    assert [row["name"] for row in apply_search(ROWS, query, ["name", "desc"])] == expected


class _Row(BaseModel):
    name: str


def test_path_tree_nests_rows_by_path_segment():
    tree = PathTree[_Row].from_paths([_Row(name="a/x"), _Row(name="b/c/y")], path=lambda r: r.name)

    assert tree.children["a"].items[0].name == "a/x"
    assert tree.children["b"].children["c"].items[0].name == "b/c/y"


def test_path_tree_reads_a_node_with_items_only():
    tree = PathTree[_Row].model_validate({"items": [{"name": "only"}], "children": {}})

    assert tree.items[0].name == "only"
