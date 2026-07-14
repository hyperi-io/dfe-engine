#  Project:      dfe-engine
#  File:         tests/gitcrud/test_registry.py
#  Purpose:      Tests for the Governed Ops resource-class registry
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for ResourceClass + the registry."""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud import (
    ResourceClass,
    UnknownResourceClassError,
    default_registry,
)


def test_default_registry_classes_are_the_rbac_prefixes():
    # deploy-repo scope: helmvars + governance + sources (the first datamodel
    # class to land; the rest arrive with multi-repo)
    assert default_registry().classes() == ["governance", "helmvars", "sources"]


def test_sources_class_registered():
    cls = default_registry().get("sources")
    assert cls.directory == "config/sources"
    assert cls.rbac_prefix == "sources"
    assert cls.versioned is True
    assert cls.action("write") == "sources:write"


def test_default_registry_governance_types_share_the_class_prefix():
    reg = default_registry()
    for type_name in ("accounts", "groups", "roles", "actions", "policies"):
        assert reg.get(type_name).rbac_prefix == "governance"
        assert reg.get(type_name).action("write") == "governance:write"


def test_helmvars_type_is_present():
    assert default_registry().get("helmvars").directory == "values"


def test_get_unknown_class_raises():
    with pytest.raises(UnknownResourceClassError):
        default_registry().get("nope")


def test_action_string_uses_rbac_prefix():
    cls = ResourceClass("helmvars", "values", rbac_prefix="helmvars")
    assert cls.action("write") == "helmvars:write"
    assert cls.action("read") == "helmvars:read"


def test_action_string_defaults_prefix_to_name():
    cls = ResourceClass("hunts", "hunts")
    assert cls.action("write") == "hunts:write"


def test_gov_settings_class_registered():
    reg = default_registry()
    cls = reg.get("gov_settings")
    assert cls.directory == "governance/settings"
    assert cls.rbac_prefix == "governance"
    assert cls.versioned is False
