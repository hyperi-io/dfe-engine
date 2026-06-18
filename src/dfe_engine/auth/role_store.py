#  Project:      dfe-engine
#  File:         role_store.py
#  Purpose:      YAML-backed CRUD for roles.yaml role definitions
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from dfe_engine.auth.roles import (
    RoleConfig,
    RoleDefinition,
    RoleResourceType,
    builtin_core_role_names,
)
from dfe_engine.core_resources.yaml_resource_type import CORE_RESOURCE_MUTATION_MESSAGE
from dfe_engine.yaml_utils import yaml_dump


class Role(BaseModel):
    """Named role with permission patterns."""

    name: str
    description: str = ""
    permissions: list[str] = Field(default_factory=list)
    scoped: bool = False
    resource_type: RoleResourceType = "custom"


class RoleStore:
    """CRUD for the single ``roles.yaml`` file (``roles:`` map)."""

    def __init__(self, roles_path: Path) -> None:
        self._path = Path(roles_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def load_config(self) -> RoleConfig:
        if not self._path.exists():
            raise FileNotFoundError(f"Roles file not found: {self._path}")
        return RoleConfig.load(self._path)

    def list(self) -> list[Role]:
        config = self.load_config()
        return [
            Role(name=name, **definition.model_dump())
            for name, definition in sorted(config.roles.items())
        ]

    def get(self, name: str) -> Role | None:
        config = self.load_config()
        definition = config.roles.get(name)
        if definition is None:
            return None
        return Role(name=name, **definition.model_dump())

    def create(
        self,
        name: str,
        *,
        description: str,
        permissions: list[str],
        scoped: bool = False,
    ) -> Role:
        config = self.load_config()
        if name in config.roles:
            raise ValueError(f"Role '{name}' already exists")
        roles = dict(config.roles)
        roles[name] = RoleDefinition(
            description=description,
            permissions=permissions,
            scoped=scoped,
            resource_type="custom",
        )
        self._write_roles(roles)
        return Role(
            name=name,
            description=description,
            permissions=permissions,
            scoped=scoped,
            resource_type="custom",
        )

    def update(
        self,
        name: str,
        *,
        description: str | None = None,
        permissions: list[str] | None = None,
        scoped: bool | None = None,
    ) -> Role:
        config = self.load_config()
        existing = config.roles.get(name)
        if existing is None:
            raise KeyError(f"Role '{name}' not found")
        if existing.resource_type == "core":
            raise ValueError(CORE_RESOURCE_MUTATION_MESSAGE)
        updated = existing.model_copy(
            update={
                k: v
                for k, v in {
                    "description": description,
                    "permissions": permissions,
                    "scoped": scoped,
                }.items()
                if v is not None
            }
        )
        roles = dict(config.roles)
        roles[name] = updated
        self._write_roles(roles)
        return Role(name=name, **updated.model_dump())

    def delete(self, name: str) -> None:
        config = self.load_config()
        if name not in config.roles:
            raise KeyError(f"Role '{name}' not found")
        existing = config.roles[name]
        if existing.resource_type == "core":
            raise ValueError(CORE_RESOURCE_MUTATION_MESSAGE)
        roles = dict(config.roles)
        del roles[name]
        self._write_roles(roles)

    def _write_roles(self, roles: dict[str, RoleDefinition]) -> RoleConfig:
        core_names = builtin_core_role_names()
        payload: dict[str, dict[str, object]] = {}
        for role_name, definition in sorted(roles.items()):
            data = definition.model_dump()
            if role_name in core_names:
                data["resource_type"] = "core"
            if not data.get("scoped"):
                data.pop("scoped", None)
            payload[role_name] = data
        yaml_dump({"roles": payload}, self._path)
        return RoleConfig.load(self._path)
