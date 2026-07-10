#  Project:      dfe-engine
#  File:         tests/unit/test_cli_auto/test_naming_units.py
#  Purpose:      Unit tests for path/verb naming derivation (naming.py)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Pure-unit coverage of ``naming.py`` - the (path, method) -> (group, verb) map.

Complements ``test_tree_naming.py`` (which asserts the built tree) by pinning the
derivation rules directly, including the edge cases the tree tests do not reach:
the empty-path root, collection-level bulk PUT/PATCH/DELETE, an item POST with no
action segment, sub-list GETs, and the kebab-casing of snake_case / CamelCase /
already-hyphenated wire names.
"""

from __future__ import annotations

from dfe_engine.cli.auto.naming import (
    _kebab,
    api_segments,
    derive_group_and_verb,
    is_param,
)


def test_is_param():
    assert is_param("{name}")
    assert is_param("{org_id}")
    assert not is_param("orgs")
    assert not is_param("{unterminated")


def test_api_segments_strips_prefix():
    assert api_segments("/api/v1/orgs") == ["orgs"]
    assert api_segments("/api/v1/auth/accounts") == ["auth", "accounts"]
    # A path that is exactly the prefix has no resource segments.
    assert api_segments("/api/v1") == []
    assert api_segments("/api/v1/") == []


def test_api_segments_without_prefix_left_intact():
    # Non-versioned paths (e.g. /health) keep all their segments.
    assert api_segments("/health") == ["health"]
    assert api_segments("/api/v2/orgs") == ["api", "v2", "orgs"]


def test_kebab_snake_and_idempotent():
    assert _kebab("reset_password") == "reset-password"
    assert _kebab("per_page") == "per-page"
    assert _kebab("name") == "name"
    # Already kebab stays kebab (idempotent).
    assert _kebab("already-kebab") == "already-kebab"


def test_kebab_camelcase_and_acronyms():
    # xform folds CamelCase / acronym runs to kebab (aws-cli/botocore convention).
    assert _kebab("displayName") == "display-name"
    assert _kebab("APIKey") == "api-key"


def test_empty_path_falls_back_to_method():
    # A path with no resource segments (just the api prefix) has no group; the
    # verb degrades to the method so the op is still addressable.
    assert derive_group_and_verb("/api/v1", "get", set()) == ([], "get")


def test_collection_crud_verbs():
    cols = {"orgs"}
    assert derive_group_and_verb("/api/v1/orgs", "get", cols) == (["orgs"], "list")
    assert derive_group_and_verb("/api/v1/orgs", "post", cols) == (["orgs"], "create")


def test_item_crud_verbs():
    cols = {"orgs"}
    assert derive_group_and_verb("/api/v1/orgs/{name}", "get", cols) == (["orgs"], "describe")
    assert derive_group_and_verb("/api/v1/orgs/{name}", "put", cols) == (["orgs"], "update")
    assert derive_group_and_verb("/api/v1/orgs/{name}", "patch", cols) == (["orgs"], "update")
    assert derive_group_and_verb("/api/v1/orgs/{name}", "delete", cols) == (["orgs"], "delete")


def test_item_post_without_action_names_after_method():
    # POST directly on an item route (no trailing action segment) is rare; it is
    # named after the method so it is at least reachable.
    cols = {"orgs"}
    assert derive_group_and_verb("/api/v1/orgs/{name}", "post", cols) == (["orgs"], "post")


def test_collection_bulk_put_patch_delete():
    # A collection that IS a known collection (has a sibling item route) supports
    # bulk update / delete verbs at the collection level.
    cols = {"orgs"}
    assert derive_group_and_verb("/api/v1/orgs", "put", cols) == (["orgs"], "update")
    assert derive_group_and_verb("/api/v1/orgs", "patch", cols) == (["orgs"], "update")
    assert derive_group_and_verb("/api/v1/orgs", "delete", cols) == (["orgs"], "delete")


def test_leaf_action_on_item_becomes_verb():
    cols = {"auth/accounts"}
    assert derive_group_and_verb(
        "/api/v1/auth/accounts/{username}/reset-password", "post", cols
    ) == (["auth", "accounts"], "reset-password")


def test_leaf_action_on_collection_becomes_verb():
    # A trailing literal that is NOT a REST collection (no sibling item route) is an
    # action verb; the group is the resource path before it.
    assert derive_group_and_verb("/api/v1/sigma/propagate", "post", set()) == (
        ["sigma"],
        "propagate",
    )


def test_sub_list_get_includes_leaf_in_group():
    # GET on a nested collection lists it; the group carries every literal segment.
    assert derive_group_and_verb("/api/v1/hunts/{id}/rules", "get", set()) == (
        ["hunts", "rules"],
        "list",
    )
