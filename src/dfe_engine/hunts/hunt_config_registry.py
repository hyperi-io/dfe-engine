"""Hunt config registry — CRUD for scheduled hunt YAML definitions.

Persists hunt configuration files under ``hunts.hunt_dir`` (first path when comma-separated).
Each hunt is stored as ``{hunt_id}.yaml``. The ``name`` field inside the YAML is the
display name used by the hunt engine at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hyperi_pylib.config import DirectoryConfigStore
from hyperi_pylib.logger import logger

from dfe_engine.yaml_utils import yaml_dump


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

    def exists(self, hunt_id: str) -> bool:
        return self._store.get(hunt_id) is not None

    def get(self, hunt_id: str) -> dict[str, Any]:
        config_data = self._store.get(hunt_id)
        if config_data is None:
            raise HuntConfigNotFoundError(f"Hunt not found: '{hunt_id}'")
        return dict(config_data)

    def save(
        self,
        hunt_id: str,
        config: dict[str, Any],
        *,
        created_by: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        yaml_path = self._hunts_directory / f"{hunt_id}.yaml"
        yaml_dump(config, yaml_path)

        if self._store.is_git:
            commit_msg = description or f"hunt: update {hunt_id}"
            if created_by:
                commit_msg = f"{commit_msg} (by {created_by})"
            self._store._git_commit(yaml_path, commit_msg, author=created_by)
            if self._store._git_push:
                self._store._git_push_remote()

        self._store._refresh_all()
        logger.info(f"Saved hunt config '{hunt_id}' → {yaml_path}")
        return config

    def delete(self, hunt_id: str) -> None:
        yaml_path = self._hunts_directory / f"{hunt_id}.yaml"

        if not yaml_path.exists():
            raise HuntConfigNotFoundError(f"Hunt not found: '{hunt_id}'")

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
                    message=f"hunt: delete {hunt_id}".encode(),
                )
                if self._store._git_push:
                    self._store._git_push_remote()
            except ValueError:
                logger.warning(
                    f"Hunts directory is outside git repo; deleting '{hunt_id}' without git commit"
                )
                yaml_path.unlink(missing_ok=True)
            except Exception as e:
                logger.error(f"Git delete failed for hunt {hunt_id}: {e}")
                yaml_path.unlink(missing_ok=True)
        else:
            yaml_path.unlink(missing_ok=True)

        with self._store._lock:
            self._store._cache.pop(hunt_id, None)

        logger.info(f"Deleted hunt config '{hunt_id}'")

    def list_hunts(self) -> list[dict[str, Any]]:
        """Return metadata dicts for all stored hunt configs."""
        results: list[dict[str, Any]] = []
        for hunt_id in self._store.list_tables():
            config_data = self._store.get(hunt_id)
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
                    "hunt_id": hunt_id,
                    "name": config.get("name", hunt_id),
                    "customers": config.get("customers", []),
                    "cron": cron_exprs[0] if len(cron_exprs) == 1 else cron_exprs,
                    "rules": rule_names,
                    "source_table": config.get("global_source_table_name", ""),
                    "target_table": config.get("global_target_table_name", ""),
                }
            )
        return results
