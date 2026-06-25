"""Rule Registry — CRUD for hunt detection rules (API-persisted YAML).

Separate from ``hunts.rule_repo_dir`` (Jinja2 templates for scheduled hunts).
Each rule is stored as ``{rule_id}.yaml`` under ``hunts.rules_dir``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hyperi_pylib.config import DirectoryConfigStore
from hyperi_pylib.logger import logger

from dfe_engine.git_identity import COMMITTER_IDENTITY, commit_file
from dfe_engine.hunts.rule_model import Rule
from dfe_engine.yaml_utils import yaml_dump


class RuleRegistryError(Exception):
    """Base exception for rule registry errors."""


class RuleNotFoundError(RuleRegistryError):
    """Rule not found in the registry."""


class RuleRegistry:
    """YAML-backed store for :class:`Rule` documents."""

    def __init__(
        self,
        rules_directory: str | Path,
        writable: bool | None = None,
        git_branch: str | None = None,
        git_push: bool = False,
        refresh_interval: int = 30,
    ) -> None:
        self._rules_directory = Path(rules_directory)
        self._rules_directory.mkdir(parents=True, exist_ok=True)

        self._store = DirectoryConfigStore(
            directory=self._rules_directory,
            refresh_interval=refresh_interval,
            writable=writable,
            git_branch=git_branch,
            git_push=git_push,
        )
        self._store.start()

    def close(self) -> None:
        if hasattr(self._store, "stop"):
            self._store.stop()

    def exists(self, rule_id: str) -> bool:
        return self._store.get(rule_id) is not None

    def get(self, rule_id: str) -> Rule:
        config_data = self._store.get(rule_id)
        if config_data is None:
            raise RuleNotFoundError(f"Rule not found: '{rule_id}'")
        return Rule.model_validate(config_data)

    def save(
        self,
        rule: Rule,
        *,
        created_by: str | None = None,
        description: str | None = None,
    ) -> Rule:
        yaml_path = self._rules_directory / f"{rule.rule_id}.yaml"
        yaml_dump(rule.model_dump(), yaml_path)

        if self._store.is_git:
            commit_msg = description or f"rule: update {rule.rule_id}"
            if created_by:
                commit_msg = f"{commit_msg} (by {created_by})"
            commit_file(self._store, yaml_path, commit_msg, author=created_by)
            if self._store._git_push:
                self._store._git_push_remote()

        self._store._refresh_all()
        logger.info(f"Saved rule '{rule.rule_id}' → {yaml_path}")
        return rule

    def delete(self, rule_id: str) -> None:
        yaml_path = self._rules_directory / f"{rule_id}.yaml"

        if not yaml_path.exists():
            raise RuleNotFoundError(f"Rule not found: '{rule_id}'")

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
                    author=COMMITTER_IDENTITY.encode("utf-8"),
                    committer=COMMITTER_IDENTITY.encode("utf-8"),
                    message=f"rule: delete {rule_id}".encode(),
                )
                if self._store._git_push:
                    self._store._git_push_remote()
            except ValueError:
                logger.warning(
                    f"Rules directory is outside git repo; deleting '{rule_id}' without git commit"
                )
                yaml_path.unlink(missing_ok=True)
            except Exception as e:
                logger.error(f"Git delete failed for rule {rule_id}: {e}")
                yaml_path.unlink(missing_ok=True)
        else:
            yaml_path.unlink(missing_ok=True)

        with self._store._lock:
            self._store._cache.pop(rule_id, None)

        logger.info(f"Deleted rule '{rule_id}'")

    def list_rules(self) -> list[dict[str, Any]]:
        """Return metadata dicts for all stored rules."""
        results: list[dict[str, Any]] = []
        for table in self._store.list_tables():
            config_data = self._store.get(table)
            if config_data is None:
                continue
            try:
                rule = Rule.model_validate(config_data)
            except Exception:
                logger.warning(f"Failed to parse rule '{table}', skipping")
                continue
            results.append(
                {
                    "rule_id": rule.rule_id,
                    "name": rule.name,
                    "severity": rule.severity,
                    "source": rule.source,
                    "source_db": rule.source_db,
                    "source_table": rule.source_table,
                    "hunt_name": rule.hunt_name,
                    "created_at": rule.created_at,
                }
            )
        return results
