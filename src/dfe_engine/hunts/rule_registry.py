"""Rule Registry -- CRUD for hunt detection rules (API-persisted YAML).

Separate from ``hunts.rule_repo_dir`` (Jinja2 templates for scheduled hunts).
Each rule is stored as ``{name}.yaml``, in one of two interchangeable backends:

- **deploy repo** (when gitops is on): the deploy repo's ``config/rules`` is the
  SSoT and every mutation is one gitcrud commit, because that is the directory
  the k8s hunt runner git-syncs. Pass ``deploy_repo=DeployRepoStore(...)``.
- **DirectoryConfigStore**: ``hunts.rules_dir`` as SSoT -- the shared config
  volume the docker hunt runner reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from scalo.config import DirectoryConfigStore
from scalo.logger import logger

from dfe_engine.git_identity import COMMITTER_IDENTITY, commit_file
from dfe_engine.hunts.hunt_config_registry import (
    resolve_display_name,
    strip_identity_fields_from_yaml,
)
from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_names import RuleNameError, rule_file
from dfe_engine.yaml_utils import yaml_dump

if TYPE_CHECKING:
    from dfe_engine.gitcrud.routing import WriteOutcome
    from dfe_engine.hunts.deploy_repo import DeployRepoStore


def _rule_to_yaml_dict(rule: Rule) -> dict[str, Any]:
    data = rule.model_dump()
    data.pop("rule_id", None)
    display = data.pop("name")
    data["display_name"] = display
    return data


def _rule_from_stored(name: str, config: dict[str, Any]) -> Rule:
    display = resolve_display_name(config, name)
    payload = strip_identity_fields_from_yaml(config)
    payload["rule_id"] = name
    payload["name"] = display
    return Rule.model_validate(payload)


class RuleRegistryError(Exception):
    """Base exception for rule registry errors."""


class RuleNotFoundError(RuleRegistryError):
    """Rule not found in the registry."""


class RuleRegistry:
    """YAML-backed store for :class:`Rule` documents."""

    def __init__(
        self,
        rules_directory: str | Path | None = None,
        writable: bool | None = None,
        git_branch: str | None = None,
        git_push: bool = False,
        refresh_interval: int = 30,
        *,
        deploy_repo: DeployRepoStore | None = None,
    ) -> None:
        """Initialise the registry.

        Args:
            rules_directory: YAML rules directory (DirectoryConfigStore backend;
                ignored when ``deploy_repo`` is given).
            writable: Whether writes are allowed. None = auto-detect.
            git_branch: Git branch for writes. None = current branch.
            git_push: Auto-push after git commits.
            refresh_interval: Seconds between background cache refresh polls.
            deploy_repo: Deploy-repo backend over the gitcrud ``rules`` class.
                When provided, ``config/rules`` in the deploy repo is the SSoT
                and every mutation is one commit the hunt runner git-syncs.
        """
        self._deploy = deploy_repo
        self._store: DirectoryConfigStore | None = None

        if deploy_repo is not None:
            self._rules_directory = deploy_repo.directory
            return

        if rules_directory is None:
            raise RuleRegistryError("rules_directory is required without a deploy repo")
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
        if self._store is not None:
            self._store.stop()

    def _require_store(self) -> DirectoryConfigStore:
        """Narrow the Optional store once: the directory backend always builds one."""
        if self._store is None:
            raise RuleRegistryError("no DirectoryConfigStore backend (deploy repo is active)")
        return self._store

    def _names(self) -> list[str]:
        """All stored rule names (file stems), which the rule compiler resolves by."""
        if self._deploy is not None:
            return self._deploy.names()
        return list(self._require_store().list_tables())

    def _get_raw(self, name: str) -> dict[str, Any] | None:
        """Raw stored doc for a rule, or None when absent."""
        if self._deploy is not None:
            return self._deploy.get(name)
        return self._require_store().get(name)

    def _put_raw(
        self, name: str, doc: dict[str, Any], *, created_by: str | None, message: str
    ) -> WriteOutcome | None:
        """Write one rule doc and log where it landed; returns the git outcome.

        Deploy repo: ONE routed commit, and the doc is stored verbatim because the
        runner's rule_compiler parses this exact file. Directory backend: plain YAML
        write, git commit when the directory is a repo, then a cache refresh, and no
        routing outcome to report.
        """
        if self._deploy is not None:
            outcome = self._deploy.put(name, doc, actor=created_by or "engine")
            logger.info(f"Saved rule '{name}' -> deploy repo config/rules")
            return outcome

        store = self._require_store()
        yaml_path = rule_file(self._rules_directory, name, ".yaml")
        yaml_dump(doc, yaml_path)
        if store.is_git:
            commit_file(store, yaml_path, message, author=created_by)
            if store._git_push:
                store._git_push_remote()
        store._refresh_all()
        logger.info(f"Saved rule '{name}' -> {yaml_path}")
        return None

    def _delete_raw(self, name: str, *, created_by: str | None) -> tuple[bool, WriteOutcome | None]:
        """Remove one rule doc; False when it did not exist, plus the git outcome."""
        if self._deploy is not None:
            outcome = self._deploy.delete(name, actor=created_by or "engine")
            return outcome is not None, outcome

        store = self._require_store()
        try:
            yaml_path = rule_file(self._rules_directory, name, ".yaml")
        except RuleNameError:
            return False, None
        if not yaml_path.exists():
            return False, None

        if store.is_git and store._repo is not None:
            try:
                from dulwich import porcelain as git

                repo_root = Path(store._repo.path).resolve(strict=False)
                yaml_abs = yaml_path.resolve(strict=False)
                rel_path = str(yaml_abs.relative_to(repo_root))
                yaml_abs.unlink(missing_ok=True)
                git.rm(store._repo, paths=[rel_path])
                git.commit(
                    store._repo,
                    author=COMMITTER_IDENTITY.encode("utf-8"),
                    committer=COMMITTER_IDENTITY.encode("utf-8"),
                    message=f"rule: delete {name}".encode(),
                )
                if store._git_push:
                    store._git_push_remote()
            except ValueError:
                logger.warning(
                    f"Rules directory is outside git repo; deleting '{name}' without git commit"
                )
                yaml_path.unlink(missing_ok=True)
            except Exception as e:
                logger.error(f"Git delete failed for rule {name}: {e}")
                yaml_path.unlink(missing_ok=True)
        else:
            yaml_path.unlink(missing_ok=True)

        with store._lock:
            store._cache.pop(name, None)
        return True, None

    # -----------------------------------------------------------------
    # CRUD Operations
    # -----------------------------------------------------------------

    def exists(self, name: str) -> bool:
        return self._get_raw(name) is not None

    def name_exists(self, name: str) -> bool:
        """True if a rule file stem is taken (exact or case-insensitive)."""
        if self.exists(name):
            return True
        key = name.casefold()
        return any(table.casefold() == key for table in self._names())

    def get(self, name: str) -> Rule:
        config_data = self._get_raw(name)
        if config_data is None:
            raise RuleNotFoundError(f"Rule not found: '{name}'")
        return _rule_from_stored(name, dict(config_data))

    def save(
        self,
        rule: Rule,
        *,
        created_by: str | None = None,
        description: str | None = None,
    ) -> WriteOutcome | None:
        """Persist one rule; returns the git routing outcome, None off gitops.

        A production+team write lands on a review branch rather than the branch the
        runner git-syncs, so the caller has to be able to say so.
        """
        commit_msg = description or f"rule: update {rule.rule_id}"
        if created_by:
            commit_msg = f"{commit_msg} (by {created_by})"

        return self._put_raw(
            rule.rule_id,
            _rule_to_yaml_dict(rule),
            created_by=created_by,
            message=commit_msg,
        )

    def delete(self, name: str, created_by: str | None = None) -> WriteOutcome | None:
        """Remove one rule; returns the git routing outcome, None off gitops."""
        found, outcome = self._delete_raw(name, created_by=created_by)
        if not found:
            raise RuleNotFoundError(f"Rule not found: '{name}'")
        logger.info(f"Deleted rule '{name}'")
        return outcome

    def list_rules(self) -> list[dict[str, Any]]:
        """Return metadata dicts for all stored rules."""
        results: list[dict[str, Any]] = []
        for table in self._names():
            config_data = self._get_raw(table)
            if config_data is None:
                continue
            try:
                rule = _rule_from_stored(table, dict(config_data))
            except Exception:
                logger.warning(f"Failed to parse rule '{table}', skipping")
                continue
            results.append(
                {
                    "name": table,
                    "display_name": rule.name,
                    "severity": rule.severity,
                    "source": rule.source,
                    "source_db": rule.source_db,
                    "source_table": rule.source_table,
                    "hunt_name": rule.hunt_name,
                    "sigma_rule_id": rule.sigma_rule_id,
                    "created_at": rule.created_at,
                }
            )
        return results
