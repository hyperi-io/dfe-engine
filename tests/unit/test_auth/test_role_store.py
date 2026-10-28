"""Tests for YAML-backed RoleStore."""

from __future__ import annotations

import pytest

from dfe_engine.auth.bootstrap import _seed_roles
from dfe_engine.auth.role_store import RoleStore
from dfe_engine.auth.roles import RoleConfig
from dfe_engine.yaml_utils import yaml_dump, yaml_load


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


class TestReapplyBuiltins:
    """A boot brings the built-in roles back to the package's grants and leaves custom roles alone."""

    def test_a_stale_built_in_is_rewritten_and_a_custom_role_kept(self, roles_path):
        store = RoleStore(roles_path)
        store.create("custom", description="Mine", permissions=["query:execute"])
        packaged = RoleConfig.load_builtin().roles["data_analyst"]
        raw = yaml_load(roles_path)
        raw["roles"]["data_analyst"] = {
            "description": packaged.description,
            "resource_type": "core",
            "permissions": ["transforms:*"],
        }
        yaml_dump(raw, roles_path)

        changed = store.reapply_builtins()

        assert "data_analyst" in changed
        assert "custom" not in changed
        analyst, custom = store.get("data_analyst"), store.get("custom")
        assert analyst is not None
        assert analyst.permissions == packaged.permissions
        assert custom is not None
        assert custom.permissions == ["query:execute"]
        assert store.get("viewer") is not None

    def test_a_current_file_is_left_untouched(self, tmp_path):
        path = tmp_path / "roles.yaml"
        _seed_roles(path)
        store = RoleStore(path)
        before = path.read_text(encoding="utf-8")

        assert store.reapply_builtins() == []
        assert path.read_text(encoding="utf-8") == before
