"""Hunt config registry — CRUD for scheduled hunt YAML definitions.

Persists hunt configuration files under ``hunts.hunt_dir`` (first path when comma-separated).
Each hunt is stored as ``{name}.yaml``. The ``display_name`` field inside the YAML is the
label used by the hunt engine at runtime (legacy YAML may use ``name`` instead).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from scalo.config import DirectoryConfigStore
from scalo.logger import logger

from dfe_engine.yaml_utils import yaml_dump

_DISPLAY_NAME_WORD = re.compile(r"[a-zA-Z0-9]+")


def default_display_name(hunt_name: str) -> str:
    """Derive a human-readable label from the hunt file stem."""
    words = _DISPLAY_NAME_WORD.findall(hunt_name.replace("_", " ").replace("-", " "))
    if not words:
        label = hunt_name
    else:
        label = " ".join(words)
    return label[0].upper() + label[1:] if label else hunt_name


def resolve_display_name(config: dict[str, Any], file_stem: str) -> str:
    """Resolve runtime / API display label from YAML (``display_name`` or legacy ``name``)."""
    for key in ("display_name", "name"):
        val = config.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return default_display_name(file_stem)


def strip_identity_fields_from_yaml(config: dict[str, Any]) -> dict[str, Any]:
    """Remove file identity keys that must not be persisted inside hunt YAML."""
    data = dict(config)
    data.pop("hunt_id", None)
    data.pop("name", None)
    return data


def alert_destination_names_from_config(config: dict[str, Any]) -> list[str]:
    """Named alert destinations referenced in hunt YAML ``alerts.destinations``."""
    alerts = config.get("alerts")
    if not isinstance(alerts, dict):
        return []
    destinations = alerts.get("destinations", [])
    if not isinstance(destinations, list):
        return []
    return [name for name in destinations if isinstance(name, str) and name]


class HuntConfigRegistryError(Exception):
    """Base exception for hunt config registry errors."""


class HuntConfigNotFoundError(HuntConfigRegistryError):
    """Hunt config not found in the registry."""


class HuntConfigRegistry:
    """YAML-backed store for hunt scheduler configuration documents."""

    def __init__(
        self,
        hunts_directory: str | Path,
        writable: bool | None = None,
        git_branch: str | None = None,
        git_push: bool = False,
        refresh_interval: int = 30,
    ) -> None:
        self._hunts_directory = Path(hunts_directory)
        self._hunts_directory.mkdir(parents=True, exist_ok=True)

        self._store = DirectoryConfigStore(
            directory=self._hunts_directory,
            refresh_interval=refresh_interval,
            writable=writable,
            git_branch=git_branch,
            git_push=git_push,
        )
        self._store.start()

    def close(self) -> None:
        if hasattr(self._store, "stop"):
            self._store.stop()

    def exists(self, name: str) -> bool:
        return self._store.get(name) is not None

    def name_exists(self, name: str) -> bool:
        """True if a hunt file stem is taken (exact or case-insensitive)."""
        if self.exists(name):
            return True
        key = name.casefold()
        return any(table.casefold() == key for table in self._store.list_tables())

    def get(self, name: str) -> dict[str, Any]:
        config_data = self._store.get(name)
        if config_data is None:
            raise HuntConfigNotFoundError(f"Hunt not found: '{name}'")
        return dict(config_data)

    def alert_destination_names_for_hunt(self, hunt_name: str) -> list[str]:
        """Destination names listed under ``alerts.destinations`` for a hunt config."""
        return alert_destination_names_from_config(self.get(hunt_name))

    def save(
        self,
        name: str,
        config: dict[str, Any],
        *,
        created_by: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        yaml_path = self._hunts_directory / f"{name}.yaml"
        payload = strip_identity_fields_from_yaml(config)
        yaml_dump(payload, yaml_path)

        if self._store.is_git:
            commit_msg = description or f"hunt: update {name}"
            if created_by:
                commit_msg = f"{commit_msg} (by {created_by})"
            self._store._git_commit(yaml_path, commit_msg, author=created_by)
            if self._store._git_push:
                self._store._git_push_remote()

        self._store._refresh_all()
        logger.info(f"Saved hunt config '{name}' → {yaml_path}")
        return payload

    def delete(self, name: str) -> None:
        yaml_path = self._hunts_directory / f"{name}.yaml"

        if not yaml_path.exists():
            raise HuntConfigNotFoundError(f"Hunt not found: '{name}'")

        if self._store.is_git and self._store._repo is not None:
            try:
                from dulwich import porcelain as git

                repo_root = Path(self._store._repo.path).resolve(strict=False)
                yaml_abs = yaml_path.resolve(strict=False)
                rel_path = str(yaml_abs.relative_to(repo_root))
                yaml_abs.unlink(missing_ok=True)
                git.rm(self._store._repo, paths=[rel_path])
                git.commit(
                    self._store._repo,
                    message=f"hunt: delete {name}".encode(),
                )
                if self._store._git_push:
                    self._store._git_push_remote()
            except ValueError:
                logger.warning(
                    f"Hunts directory is outside git repo; deleting '{name}' without git commit"
                )
                yaml_path.unlink(missing_ok=True)
            except Exception as e:
                logger.error(f"Git delete failed for hunt {name}: {e}")
                yaml_path.unlink(missing_ok=True)
        else:
            yaml_path.unlink(missing_ok=True)

        with self._store._lock:
            self._store._cache.pop(name, None)

        logger.info(f"Deleted hunt config '{name}'")

    def list_hunts(self) -> list[dict[str, Any]]:
        """Return metadata dicts for all stored hunt configs."""
        results: list[dict[str, Any]] = []
        for name in self._store.list_tables():
            config_data = self._store.get(name)
            if config_data is None:
                continue
            config = dict(config_data)
            cron = config.get("cron", [])
            if isinstance(cron, str):
                cron_exprs = [cron]
            else:
                cron_exprs = list(cron) if cron else []
            rule_names = [
                r.get("rule_name", "") for r in config.get("rules", []) if isinstance(r, dict)
            ]
            results.append(
                {
                    "name": name,
                    "display_name": resolve_display_name(config, name),
                    "customers": config.get("customers", []),
                    "cron": cron_exprs[0] if len(cron_exprs) == 1 else cron_exprs,
                    "rules": rule_names,
                    "source_table": config.get("global_source_table_name", ""),
                    "target_table": config.get("global_target_table_name", ""),
                }
            )
        return results

    def hunt_names_referencing_rule(self, rule_name: str) -> list[str]:
        """Hunt config file stems whose ``rules`` list includes ``rule_name``."""
        return [row["name"] for row in self.list_hunts() if rule_name in (row.get("rules") or [])]

    def hunt_names_referencing_destination(self, destination_name: str) -> list[str]:
        """Hunt configs whose ``alerts.destinations`` includes ``destination_name``."""
        names: list[str] = []
        for hunt_name in self._store.list_tables():
            config_data = self._store.get(hunt_name)
            if config_data is None:
                continue
            if destination_name in alert_destination_names_from_config(config_data):
                names.append(hunt_name)
        return names
