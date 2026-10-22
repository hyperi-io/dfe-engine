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
    # deploy-repo scope: helmvars + governance + library + sources + schema (the
    # derived column selections those sources name, and the meta schemas the API
    # writes) + the hunt and rule classes the hunt runner git-syncs + the config
    # every engine replica reads (field maps, alert destinations, service configs)
    assert default_registry().classes() == [
        "alert",
        "fieldmap",
        "governance",
        "helmvars",
        "hunt",
        "library",
        "rule",
        "schema",
        "service",
        "sources",
    ]


@pytest.mark.parametrize(
    ("name", "directory", "prefix", "nested"),
    [
        ("meta_schemas", "config/schemas", "schema", True),
        ("fieldmaps", "config/fieldmaps", "fieldmap", True),
        ("alert_destinations", "config/alert-destinations", "alert", False),
        ("service_configs", "config/services", "service", False),
    ],
)
def test_replica_config_classes_bind_the_existing_grants(name, directory, prefix, nested):
    """Each reuses the grant its API route already checks, so no role changes."""
    cls = default_registry().get(name)
    assert (cls.directory, cls.rbac_prefix, cls.nested) == (directory, prefix, nested)
    assert cls.versioned is False


def test_hunt_classes_are_registered_at_the_paths_the_runner_syncs():
    reg = default_registry()
    hunts, rules = reg.get("hunts"), reg.get("rules")
    assert (hunts.directory, rules.directory) == ("config/hunts", "config/rules")
    assert (hunts.versioned, rules.versioned) == (False, False)
    assert hunts.action("write") == "hunt:write"
    assert rules.action("write") == "rule:write"


def test_library_class_registered():
    cls = default_registry().get("library")
    assert cls.directory == "config/library"
    assert cls.rbac_prefix == "library"
    assert cls.versioned is True
    assert cls.action("write") == "library:write"


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
