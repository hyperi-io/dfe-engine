"""Tests for YAML-backed RoleStore."""

from __future__ import annotations

import pytest

from dfe_engine.auth.role_store import RoleStore
from dfe_engine.yaml_utils import yaml_dump


@pytest.fixture
def roles_path(tmp_path):
    path = tmp_path / "roles.yaml"
    yaml_dump(
        {
            "roles": {
                "viewer": {
                    "description": "Read only",
                    "permissions": ["config:read", "source:read"],
                },
            }
        },
        path,
    )
    return path


class TestRoleStore:
    def test_list_and_get(self, roles_path):
        store = RoleStore(roles_path)
        roles = store.list()
        assert len(roles) == 1
        assert roles[0].name == "viewer"
        assert store.get("viewer") is not None
        assert store.get("missing") is None

    def test_create_update_delete(self, roles_path):
        store = RoleStore(roles_path)
        created = store.create(
            "custom",
            description="Custom role",
            permissions=["query:execute"],
            scoped=True,
        )
        assert created.scoped is True
        assert created.resource_type == "custom"
        assert store.get("custom") is not None

        updated = store.update("custom", description="Updated")
        assert updated.description == "Updated"

        store.delete("custom")
        assert store.get("custom") is None

    def test_create_duplicate_raises(self, roles_path):
        store = RoleStore(roles_path)
        with pytest.raises(ValueError, match="already exists"):
            store.create("viewer", description="", permissions=[])
