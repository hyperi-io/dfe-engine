#  Purpose:      Detect core (non-editable) resources for API write guards
#  License:      BUSL-1.1

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from dfe_engine.core_resources.yaml_resource_type import (
    CORE_RESOURCE_MUTATION_MESSAGE,
    config_is_core,
)
from dfe_engine.fieldmap.models import DEFAULT_MAP_NAME

if TYPE_CHECKING:
    from dfe_engine.auth.role_store import RoleStore
    from dfe_engine.fieldmap.registry import FieldMapRegistry
    from dfe_engine.schema.registry import SchemaRegistry

MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def core_resource_conflict_message() -> str:
    return CORE_RESOURCE_MUTATION_MESSAGE


def schema_registry_path_is_core(
    schema_path: str,
    registry: SchemaRegistry | None = None,
) -> bool:
    if registry is None:
        return False
    from dfe_engine.schema.registry import SchemaValidationError, canonical_schema_path

    try:
        canonical = canonical_schema_path(schema_path)
    except SchemaValidationError:
        return False

    table = registry._table_name(canonical)
    config = registry._store.get(table)
    if not isinstance(config, dict):
        return False
    return config_is_core(config)


def role_name_is_core(name: str, role_store: RoleStore | None) -> bool:
    if role_store is None:
        return False
    role = role_store.get(name)
    return role is not None and role.resource_type == "core"


def fieldmap_target_is_core(
    standard: str | None,
    source: str | None,
    registry: FieldMapRegistry | None,
) -> bool:
    if registry is None or not standard:
        return False
    src = source if source not in (None, "", DEFAULT_MAP_NAME) else None
    if not registry.map_exists(standard, source=src):
        return False
    table = registry._table_name(standard, src)
    config = registry._store.get(table)
    if not isinstance(config, dict):
        return False
    return config_is_core(config)


def parse_fieldmap_post_body(body: bytes) -> tuple[str | None, str | None]:
    if not body:
        return None, None
    try:
        data: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError:
        return None, None
    standard = data.get("standard")
    source = data.get("source")
    if isinstance(standard, str):
        standard = standard.lower()
    else:
        standard = None
    if isinstance(source, str):
        source = source.lower()
    else:
        source = None
    return standard, source


def _fieldmap_mutation_conflict(
    *,
    method: str,
    path: str,
    body: bytes,
    fieldmap_registry: FieldMapRegistry | None,
) -> str | None:
    prefix = "/api/v1/field-maps"
    if not path.startswith(prefix):
        return None
    if path == f"{prefix}/seed":
        return None

    if path == prefix and method == "POST":
        standard, source = parse_fieldmap_post_body(body)
        if fieldmap_target_is_core(standard, source, fieldmap_registry):
            label = standard if source is None else f"{standard}/{source}"
            return core_resource_conflict_message()
        return None

    rest = path[len(prefix) :].strip("/")
    if not rest or method not in MUTATION_METHODS:
        return None

    parts = [unquote(p) for p in rest.split("/")]
    if method == "DELETE" and len(parts) >= 2:
        standard, source = parts[0].lower(), parts[1].lower()
        if fieldmap_target_is_core(standard, source, fieldmap_registry):
            return core_resource_conflict_message()
    return None


def _schema_mutation_conflict(
    *,
    method: str,
    path: str,
    schema_registry: SchemaRegistry | None,
) -> str | None:
    definitions = "/api/v1/schemas/definitions/"
    if not path.startswith(definitions):
        return None
    rest = path[len(definitions) :].strip("/")
    if not rest:
        return None

    if rest.endswith("/versions"):
        schema_path = unquote(rest[: -len("/versions")].strip("/"))
        if method == "POST" and schema_registry_path_is_core(schema_path, schema_registry):
            return core_resource_conflict_message()
        return None

    if "/versions/" in rest:
        return None

    schema_path = unquote(rest)
    if schema_registry_path_is_core(schema_path, schema_registry):
        return core_resource_conflict_message()
    return None


def _role_mutation_conflict(
    *,
    method: str,
    path: str,
    role_store: RoleStore | None,
) -> str | None:
    prefix = "/api/v1/auth/roles/"
    if not path.startswith(prefix):
        return None
    suffix = path[len(prefix) :].strip("/")
    if not suffix or suffix == "scopes":
        return None
    name = unquote(suffix.split("/")[0])
    if method in {"PUT", "DELETE"} and role_name_is_core(name, role_store):
        return core_resource_conflict_message()
    return None


def match_api_core_mutation(
    *,
    method: str,
    path: str,
    role_store: RoleStore | None,
    schema_registry: SchemaRegistry | None,
    fieldmap_registry: FieldMapRegistry | None = None,
    body: bytes = b"",
) -> str | None:
    """Return a conflict message if this request mutates a core resource."""
    if method not in MUTATION_METHODS:
        return None

    for check in (
        lambda: _role_mutation_conflict(method=method, path=path, role_store=role_store),
        lambda: _schema_mutation_conflict(
            method=method, path=path, schema_registry=schema_registry
        ),
        lambda: _fieldmap_mutation_conflict(
            method=method,
            path=path,
            body=body,
            fieldmap_registry=fieldmap_registry,
        ),
    ):
        message = check()
        if message:
            return message
    return None
