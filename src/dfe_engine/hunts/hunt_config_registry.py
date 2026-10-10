"""Hunt config registry -- CRUD for scheduled hunt YAML definitions.

Two interchangeable backends behind one registry surface:

- **deploy repo** (when gitops is on): the deploy repo's ``config/hunts`` is the
  SSoT and every mutation is one gitcrud commit, because that is the directory
  the k8s hunt runner git-syncs. Pass ``deploy_repo=DeployRepoStore(...)``.
- **DirectoryConfigStore**: ``hunts.hunt_dir`` (first path when comma-separated)
  as SSoT -- the shared config volume the docker hunt runner reads.

Each hunt is stored as ``{name}.yaml``. The ``display_name`` field inside the YAML is the
label used by the hunt engine at runtime (legacy YAML may use ``name`` instead).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scalo.config import DirectoryConfigStore
from scalo.logger import logger

from dfe_engine.yaml_utils import contained_yaml_file, yaml_dump

if TYPE_CHECKING:
    from dfe_engine.gitcrud.routing import WriteOutcome
    from dfe_engine.hunts.deploy_repo import DeployRepoStore

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
        hunts_directory: str | Path | None = None,
        writable: bool | None = None,
        git_branch: str | None = None,
        git_push: bool = False,
        refresh_interval: int = 30,
        *,
        deploy_repo: DeployRepoStore | None = None,
    ) -> None:
        """Initialise the registry.

        Args:
            hunts_directory: YAML hunt directory (DirectoryConfigStore backend;
                ignored when ``deploy_repo`` is given).
            writable: Whether writes are allowed. None = auto-detect.
            git_branch: Git branch for writes. None = current branch.
            git_push: Auto-push after git commits.
            refresh_interval: Seconds between background cache refresh polls.
            deploy_repo: Deploy-repo backend over the gitcrud ``hunts`` class.
                When provided, ``config/hunts`` in the deploy repo is the SSoT
                and every mutation is one commit the hunt runner git-syncs.
        """
        self._deploy = deploy_repo
        self._store: DirectoryConfigStore | None = None

        if deploy_repo is not None:
            self._hunts_directory = deploy_repo.directory
            return

        if hunts_directory is None:
            raise HuntConfigRegistryError("hunts_directory is required without a deploy repo")
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
        if self._store is not None:
            self._store.stop()

    def _require_store(self) -> DirectoryConfigStore:
        """Narrow the Optional store once: the directory backend always builds one."""
        if self._store is None:
            raise HuntConfigRegistryError("no DirectoryConfigStore backend (deploy repo is active)")
        return self._store

    def _names(self) -> list[str]:
        """All stored hunt names (file stems), which are the runner's hunt ids."""
        if self._deploy is not None:
            return self._deploy.names()
        return list(self._require_store().list_tables())

    def _get_raw(self, name: str) -> dict[str, Any] | None:
        """Raw stored doc for a hunt, or None when absent.

        The store strips a leading ``/`` before it looks a name up, so a name whose
        file would sit outside the directory is absent here, as it is to a write.
        """
        if self._deploy is not None:
            return self._deploy.get(name)
        if contained_yaml_file(self._hunts_directory, name) is None:
            return None
        return self._require_store().get(name)

    def _put_raw(
        self, name: str, doc: dict[str, Any], *, created_by: str | None, message: str
    ) -> WriteOutcome | None:
        """Write one hunt doc and log where it landed; returns the git outcome.

        Deploy repo: ONE routed commit, and the doc is stored verbatim because the
        runner's spec_loader parses this exact file. Directory backend: plain YAML
        write, git commit when the directory is a repo, then a cache refresh, and no
        routing outcome to report.
        """
        if self._deploy is not None:
            outcome = self._deploy.put(name, doc, actor=created_by or "engine")
            logger.info(f"Saved hunt config '{name}' -> deploy repo config/hunts")
            return outcome

        store = self._require_store()
        yaml_path = contained_yaml_file(self._hunts_directory, name)
        if yaml_path is None:
            raise HuntConfigRegistryError(
                f"Hunt name {name!r} resolves outside the hunts directory"
            )
        yaml_dump(doc, yaml_path)
        if store.is_git:
            store._git_commit(yaml_path, message, author=created_by)
            if store._git_push:
                store._git_push_remote()
        store._refresh_all()
        logger.info(f"Saved hunt config '{name}' -> {yaml_path}")
        return None

    def _delete_raw(self, name: str, *, created_by: str | None) -> tuple[bool, WriteOutcome | None]:
        """Remove one hunt doc; False when it did not exist, plus the git outcome."""
        if self._deploy is not None:
            outcome = self._deploy.delete(name, actor=created_by or "engine")
            return outcome is not None, outcome

        store = self._require_store()
        yaml_path = contained_yaml_file(self._hunts_directory, name)
        if yaml_path is None or not yaml_path.exists():
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
                    message=f"hunt: delete {name}".encode(),
                )
                if store._git_push:
                    store._git_push_remote()
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

        with store._lock:
            store._cache.pop(name, None)
        return True, None

    # -----------------------------------------------------------------
    # CRUD Operations
    # -----------------------------------------------------------------

    def exists(self, name: str) -> bool:
        return self._get_raw(name) is not None

    def name_exists(self, name: str) -> bool:
        """True if a hunt file stem is taken (exact or case-insensitive)."""
        if self.exists(name):
            return True
        key = name.casefold()
        return any(table.casefold() == key for table in self._names())

    def get(self, name: str) -> dict[str, Any]:
        config_data = self._get_raw(name)
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
    ) -> WriteOutcome | None:
        """Persist one hunt config; returns the git routing outcome, None off gitops.

        A production+team write lands on a review branch rather than the branch the
        runner git-syncs, so the caller has to be able to say so.
        """
        payload = strip_identity_fields_from_yaml(config)
        commit_msg = description or f"hunt: update {name}"
        if created_by:
            commit_msg = f"{commit_msg} (by {created_by})"

        return self._put_raw(name, payload, created_by=created_by, message=commit_msg)

    def delete(self, name: str, created_by: str | None = None) -> WriteOutcome | None:
        """Remove one hunt config; returns the git routing outcome, None off gitops."""
        found, outcome = self._delete_raw(name, created_by=created_by)
        if not found:
            raise HuntConfigNotFoundError(f"Hunt not found: '{name}'")
        logger.info(f"Deleted hunt config '{name}'")
        return outcome

    def list_hunts(self) -> list[dict[str, Any]]:
        """Return metadata dicts for all stored hunt configs."""
        results: list[dict[str, Any]] = []
        for name in self._names():
            config_data = self._get_raw(name)
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
        for hunt_name in self._names():
            config_data = self._get_raw(hunt_name)
            if config_data is None:
                continue
            if destination_name in alert_destination_names_from_config(config_data):
                names.append(hunt_name)
        return names
