#  Project:      dfe-engine
#  File:         tests/unit/test_cli_auto/test_build_units.py
#  Purpose:      Click option construction + group/command collision handling
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""``build`` option minting + the silent-drop collision guards.

``test_tree_naming`` asserts the built tree against the live spec; this pins the
per-type option construction (bool flag / int / float / multiple / Choice) and the
two collision guards that keep a command from being silently overwritten: a leaf
command displaced when a group is needed at its name, and two ops colliding on the
same ``(group, verb)`` being disambiguated by method.
"""

from __future__ import annotations

import click

from dfe_engine.cli.auto.build import _ensure_group, _option_for, build_command_tree
from dfe_engine.cli.auto.spec import BodyProp, Operation, Param


def _op(**over) -> Operation:
    base = {
        "path": "/api/v1/res/act",
        "method": "post",
        "path_params": [],
        "query_params": [],
        "body_props": [],
        "success_response_schema": None,
        "response_ref_name": None,
        "summary": "",
        "description": "",
        "group_path": ["res"],
        "verb": "action",
        "freeform_body": False,
    }
    base.update(over)
    return Operation(**base)


# --- option construction per type --------------------------------------------


def test_bool_prop_becomes_flag_pair():
    opt = _option_for(BodyProp("flag", "flag", "bool", False, default=False), required=False)
    assert opt.is_bool_flag
    assert "--flag" in opt.opts
    assert "--no-flag" in opt.secondary_opts


def test_int_and_float_types():
    assert _option_for(Param("count", "count", "int", False), required=False).type is click.INT
    assert _option_for(Param("ratio", "ratio", "float", False), required=False).type is click.FLOAT


def test_array_prop_is_multiple():
    opt = _option_for(BodyProp("labels", "labels", "array", False), required=False)
    assert opt.multiple is True


def test_enum_str_becomes_choice():
    opt = _option_for(Param("mode", "mode", "str", False, enum=["a", "b"]), required=False)
    assert isinstance(opt.type, click.Choice)
    assert list(opt.type.choices) == ["a", "b"]


def test_required_is_propagated():
    assert _option_for(Param("name", "name", "str", True), required=True).required is True


# --- collision guards --------------------------------------------------------


def test_ensure_group_displaces_a_conflicting_leaf_command():
    root = click.Group("dfe")
    root.add_command(click.Command("x", callback=lambda: None))
    leaf = _ensure_group(root, ["x"])
    # The name `x` is now a GROUP; the original command is kept under `x-cmd`.
    assert isinstance(root.commands["x"], click.Group)
    assert isinstance(root.commands["x-cmd"], click.Command)
    assert leaf is root.commands["x"]


def test_command_collision_disambiguated_by_method():
    root = click.Group("dfe")
    build_command_tree(root, [_op(method="post"), _op(method="get")])
    group = root.commands["res"]
    # Neither op is lost: the second keeps its verb suffixed with the method.
    assert "action" in group.commands
    assert "action-get" in group.commands
