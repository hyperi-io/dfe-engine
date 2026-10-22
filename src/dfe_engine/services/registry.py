"""Service configuration registry over the deploy repo or a YAML directory.

Storage model:
- With gitops on, the deploy repo's ``service_configs`` class is the Single
  Source of Truth (SSoT): every engine replica reads it through its own clone and
  every write is one commit routed by the deployment's posture.
- Without gitops, a YAML directory is the SSoT, behind scalo's
  DirectoryConfigStore (cached reads, background refresh, optional git-aware
  writes, change callbacks). The Rust services on Compose read these files.
- Config history is the git log of the config's file, in either backend.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from dulwich.repo import Repo
from pydantic import BaseModel, SecretBytes, SecretStr
from pydantic_core import to_jsonable_python
from scalo.config import DirectoryConfigStore
from scalo.logger import logger

from dfe_engine.git_identity import COMMITTER_IDENTITY, commit_file, git_repo_relative_path
from dfe_engine.services.plugins import get_plugin, valid_services
from dfe_engine.services.validators import ValidationResult, validate_config
from dfe_engine.yaml_utils import yaml_dump, yaml_load_string

if TYPE_CHECKING:
    from dfe_engine.gitcrud.routing import ReviewRouting
    from dfe_engine.hunts.deploy_repo import DeployRepoStore

SERVICE_CONFIG_CLASS = "service_configs"
"""The gitcrud resource class service configs live in when gitops is on."""


def _unwrapped(value: Any) -> Any:
    """``value`` with every pydantic secret replaced by what it holds."""
    if isinstance(value, SecretStr | SecretBytes):
        return value.get_secret_value()
    if isinstance(value, dict):
        return {key: _unwrapped(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_unwrapped(item) for item in value]
    return value


def stored_form(config: BaseModel | dict[str, Any]) -> dict[str, Any]:
    """A config as its YAML file holds it, every secret written out in full.

    The Rust services read that file, so a secret in it is the secret itself. The
    JSON dump a read returns shows pydantic's placeholder instead.
    """
    if isinstance(config, dict):
        return config
    return to_jsonable_python(_unwrapped(config.model_dump()))


def _file_history(repo: Repo, rel_path: str, limit: int) -> list[dict[str, Any]]:
    """The newest commits that changed one repo-relative file, newest first."""
    from dulwich.objects import Commit, Tree
    from dulwich.walk import Walker

    path = rel_path.encode("utf-8")
    try:
        head = repo.head()
    except KeyError:
        return []
    history: list[dict[str, Any]] = []
    try:
        for entry in Walker(repo.object_store, [head]):
            if len(history) >= limit:
                break
            commit = entry.commit
            tree = cast(Tree, repo[commit.tree])
            try:
                current_entry = tree.lookup_path(repo.__getitem__, path)
            except KeyError:
                continue
            if commit.parents:
                parent = cast(Commit, repo[commit.parents[0]])
                parent_tree = cast(Tree, repo[parent.tree])
                try:
                    if parent_tree.lookup_path(repo.__getitem__, path)[1] == current_entry[1]:
                        continue
                except KeyError:
                    pass  # added in this commit
            author_str = commit.author.decode("utf-8", errors="replace")
            author_name = author_str.split("<")[0].strip() if "<" in author_str else author_str
            history.append(
                {
                    "commit": commit.id.decode("ascii"),
                    "message": commit.message.decode("utf-8", errors="replace").strip(),
                    "author": author_name,
                    "date": datetime.fromtimestamp(commit.author_time, tz=UTC).isoformat(),
                }
            )
    except Exception as e:
        logger.warning(f"Failed to read git history for {rel_path}: {e}")
        return []
    return history


class ServiceConfigError(Exception):
    """Base exception for service config errors."""


class ConfigNotFoundError(ServiceConfigError):
    """Configuration not found for service/instance."""


class ServiceConfigRegistry:
    """Registry for managing DFE Rust service configurations.

    Backed by the deploy repo's ``service_configs`` class or a YAML directory.

    Layout, under either root:
        <config_directory>/
            receiver-default.yaml
            receiver-production.yaml
            loader-default.yaml
            loader-staging.yaml
            archiver-default.yaml
            ...

    Each file is named ``{service}-{instance}.yaml`` and contains the full
    configuration for that service instance as a YAML document.

    Supports multiple instances per service (e.g., 'production', 'staging').
    """

    _instance: ServiceConfigRegistry | None = None

    def __init__(
        self,
        config_directory: str | Path | None = None,
        writable: bool | None = None,
        git_branch: str | None = None,
        git_push: bool = False,
        refresh_interval: int = 30,
        *,
        deploy_repo: DeployRepoStore | None = None,
    ) -> None:
        """Initialize the registry.

        Args:
            config_directory: Path to the YAML config directory. Ignored when
                ``deploy_repo`` is given.
            writable: Whether writes are allowed. None = auto-detect.
            git_branch: Git branch for writes. None = current branch.
            git_push: Auto-push after git commits.
            refresh_interval: Seconds between background cache refresh polls.
            deploy_repo: The deploy repo's ``service_configs`` class.

        Raises:
            ServiceConfigError: Neither backend was given.
        """
        self._deploy = deploy_repo
        self._store: DirectoryConfigStore | None = None
        if deploy_repo is not None:
            self._config_directory = deploy_repo.directory
            return
        if config_directory is None:
            raise ServiceConfigError("config_directory is required without a deploy repo")
        self._config_directory = Path(config_directory).resolve()
        self._config_directory.mkdir(parents=True, exist_ok=True)

        self._store = DirectoryConfigStore(
            directory=self._config_directory,
            refresh_interval=refresh_interval,
            writable=writable,
            git_branch=git_branch,
            git_push=git_push,
        )
        self._store.start()

    def _require_store(self) -> DirectoryConfigStore:
        """The directory backend's store, which a deploy-repo registry does not have."""
        if self._store is None:
            raise ServiceConfigError("no DirectoryConfigStore backend (deploy repo is active)")
        return self._store

    def _tables(self) -> list[str]:
        """Every stored ``{service}-{instance}`` key."""
        if self._deploy is not None:
            return self._deploy.names()
        return list(self._require_store().list_tables())

    def _get_raw(self, table: str) -> dict[str, Any] | None:
        """The stored doc for one key, or None when absent."""
        if self._deploy is not None:
            return self._deploy.get(table)
        return self._require_store().get(table)

    @classmethod
    def get_instance(
        cls,
        config_directory: str | Path | None = None,
        writable: bool | None = None,
        git_branch: str | None = None,
        git_push: bool = False,
        refresh_interval: int = 30,
    ) -> ServiceConfigRegistry:
        """Get singleton registry instance."""
        if cls._instance is None:
            if config_directory is None:
                raise ServiceConfigError(
                    "config_directory is required on first call to get_instance()"
                )
            cls._instance = ServiceConfigRegistry(
                config_directory=config_directory,
                writable=writable,
                git_branch=git_branch,
                git_push=git_push,
                refresh_interval=refresh_interval,
            )
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton (for testing)."""
        if cls._instance:
            cls._instance.close()
        cls._instance = None

    # -------------------------------------------------------------------------
    # Table naming convention
    # -------------------------------------------------------------------------

    @staticmethod
    def _table_name(service: str, instance: str) -> str:
        """Map service + instance to a DirectoryConfigStore table name.

        The table name is the YAML filename without extension.
        """
        return f"{service}-{instance}"

    @staticmethod
    def _parse_table_name(table: str) -> tuple[str, str] | None:
        """Parse a table name back into (service, instance).

        Tries known service prefixes first (handles hyphenated names like
        'transform-wasm'). Falls back to splitting on the last hyphen for
        services not registered as plugins (schema-less mode).
        """
        for svc in sorted(valid_services(), key=len, reverse=True):
            prefix = f"{svc}-"
            if table.startswith(prefix):
                instance = table[len(prefix) :]
                if instance:
                    return svc, instance
        # Unknown service: split on last hyphen so any "{service}-{instance}"
        # file is included in listings (e.g. a new Rust service not yet registered)
        idx = table.rfind("-")
        if idx > 0 and idx < len(table) - 1:
            return table[:idx], table[idx + 1 :]
        return None

    # -------------------------------------------------------------------------
    # CRUD Operations
    # -------------------------------------------------------------------------

    def get_config(self, service: str, instance: str = "default"):
        """Get a service configuration.

        Reads from the DirectoryConfigStore in-memory cache (backed by YAML).
        For registered services, returns a typed Pydantic model. For unknown
        services (schema-less mode), returns the raw dict.

        Args:
            service: Service name (e.g. 'receiver', 'loader', or any new service)
            instance: Deployment instance name (e.g., 'default', 'production')

        Returns:
            Typed configuration model for registered services, raw dict otherwise.

        Raises:
            ConfigNotFoundError: Config not found
        """
        table = self._table_name(service, instance)

        config_data = self._get_raw(table)
        if config_data is None:
            raise ConfigNotFoundError(f"Config not found for {service}/{instance}")

        try:
            plugin = get_plugin(service)
            return plugin.config_class.model_validate(config_data)
        except KeyError:
            # Unknown service: return the raw dict (schema-less mode)
            return config_data

    def save_config(
        self,
        service: str,
        config,
        instance: str = "default",
        created_by: str | None = None,
        description: str | None = None,
        *,
        routing: ReviewRouting | None = None,
    ) -> None:
        """Save a service configuration.

        The deploy repo takes it as one routed commit. A directory takes a plain
        file write, auto-committed when the directory is a git repo.

        Args:
            service: Service name
            config: Configuration model or dict
            instance: Deployment instance name
            created_by: Username/identity of who made the change
            description: Description of the change
            routing: Collects where a deploy-repo write landed.
        """
        # Normalize to dict. For registered services, validate via typed model.
        # For unknown services (schema-less mode), store the raw dict as-is.
        if isinstance(config, dict):
            try:
                plugin = get_plugin(service)
                config_data = stored_form(plugin.config_class.model_validate(config))
            except KeyError:
                # Unknown service: store as-is
                config_data = config
        else:
            config_data = stored_form(config)

        table = self._table_name(service, instance)

        if self._deploy is not None:
            outcome = self._deploy.put(table, config_data, actor=created_by or "engine")
            if routing is not None:
                routing.record(outcome)
            logger.info(f"Saved config for {service}/{instance} -> deploy repo")
            return

        store = self._require_store()
        # Write YAML file directly (full document replacement)
        yaml_path = self._config_directory / f"{table}.yaml"
        yaml_dump(config_data, yaml_path)

        # Git commit if the store is git-aware
        if store.is_git:
            commit_msg = description or f"config: update {service}/{instance}"
            if created_by:
                commit_msg = f"{commit_msg} (by {created_by})"
            commit_file(store, yaml_path, commit_msg, author=created_by)
            if store._git_push:
                store._git_push_remote()

        # Force cache refresh for this table
        store._refresh_all()

        logger.info(f"Saved config for {service}/{instance} -> {yaml_path}")

    def delete_config(
        self,
        service: str,
        instance: str = "default",
        *,
        created_by: str | None = None,
        routing: ReviewRouting | None = None,
    ) -> None:
        """Delete a service configuration.

        The deploy repo takes the delete as one routed commit. A directory removes
        the YAML file and commits the deletion if git-aware.

        Args:
            service: Service name (any, including unregistered services)
            instance: Deployment instance name
            created_by: Username/identity, recorded on a deploy-repo commit.
            routing: Collects where a deploy-repo write landed.
        """
        table = self._table_name(service, instance)
        if self._deploy is not None:
            outcome = self._deploy.delete(table, actor=created_by or "engine")
            if outcome is None:
                logger.warning(f"Config does not exist: {service}/{instance}")
                return
            if routing is not None:
                routing.record(outcome)
            logger.info(f"Deleted config for {service}/{instance}")
            return

        store = self._require_store()
        yaml_path = self._config_directory / f"{table}.yaml"

        if not yaml_path.exists():
            logger.warning(f"Config file does not exist: {yaml_path}")
            return

        resolved_path = yaml_path.resolve(strict=False)
        resolved_path.unlink(missing_ok=True)

        if store.is_git and store._repo is not None:
            try:
                from dulwich import porcelain as git

                rel_path = git_repo_relative_path(store._repo.path, resolved_path)
                git.rm(store._repo, paths=[rel_path])
                git.commit(
                    store._repo,
                    author=COMMITTER_IDENTITY.encode("utf-8"),
                    committer=COMMITTER_IDENTITY.encode("utf-8"),
                    message=f"config: delete {service}/{instance}".encode(),
                )
                if store._git_push:
                    store._git_push_remote()
            except Exception as e:
                logger.error(f"Git delete failed: {e}")

        with store._lock:
            store._cache.pop(table, None)

        logger.info(f"Deleted config for {service}/{instance}")

    def list_configs(self, service: str | None = None) -> list[dict[str, Any]]:
        """List all stored configurations.

        Args:
            service: Optional filter by service name

        Returns:
            List of config metadata dicts (service, instance, updated_at)
        """
        results = []
        for table in self._tables():
            parsed = self._parse_table_name(table)
            if parsed is None:
                continue
            svc, inst = parsed
            if service and svc != service:
                continue

            yaml_path = self._config_directory / f"{table}.yaml"
            try:
                stat = yaml_path.stat()
                updated_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
            except OSError:
                updated_at = None

            results.append(
                {
                    "service": svc,
                    "instance": inst,
                    "updated_at": updated_at,
                }
            )

        return results

    # -------------------------------------------------------------------------
    # Validation
    # -------------------------------------------------------------------------

    def validate(self, service: str, config_data: dict) -> ValidationResult:
        """Validate a configuration without saving (dry-run).

        For registered services, runs Pydantic + cross-field validation.
        For unknown services (schema-less mode), returns valid with a warning.

        Args:
            service: Service name
            config_data: Configuration dictionary

        Returns:
            ValidationResult with errors and warnings
        """
        result = validate_config(service, config_data)
        if not result.valid and result.errors and result.errors[0].startswith("Unknown service:"):
            # Unknown service: no schema to validate against, treat as valid
            return ValidationResult(
                valid=True, warnings=[f"No schema registered for service '{service}'; stored as-is"]
            )
        return result

    # -------------------------------------------------------------------------
    # History (git log)
    # -------------------------------------------------------------------------

    def get_config_history(
        self, service: str, instance: str = "default", limit: int = 10
    ) -> list[dict[str, Any]]:
        """Get configuration change history from git log.

        Available from the deploy repo, or from a config directory inside a git
        repo. Uses dulwich to walk the commit history.

        Args:
            service: Service name
            instance: Deployment instance name
            limit: Maximum number of history entries

        Returns:
            List of history entries (commit, message, author, date)
        """
        self._validate_service(service)
        table = self._table_name(service, instance)

        if self._deploy is not None:
            rel_path = self._deploy.relative_path(table)
            # The clone takes the remote's head first, so a write on another replica shows.
            with self._deploy.reading(), Repo(str(self._deploy.repo_path)) as repo:
                return _file_history(repo, rel_path, limit)

        store = self._require_store()
        if not store.is_git or store._repo is None:
            logger.warning("Config history requires a git-backed config directory")
            return []
        yaml_path = (self._config_directory / f"{table}.yaml").resolve(strict=False)
        rel_path = git_repo_relative_path(store._repo.path, yaml_path)
        return _file_history(store._repo, rel_path, limit)

    # -------------------------------------------------------------------------
    # YAML Export
    # -------------------------------------------------------------------------

    def export_yaml(self, service: str, instance: str, path: Path) -> Path:
        """Export a configuration to a standalone YAML file.

        Args:
            service: Service name
            instance: Deployment instance name
            path: Output file path

        Returns:
            Path to the written file
        """
        config_data = stored_form(self.get_config(service, instance))
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        yaml_dump(config_data, path)
        logger.info(f"Exported {service}/{instance} config to {path}")
        return path

    # -------------------------------------------------------------------------
    # Git Operations (passthrough)
    # -------------------------------------------------------------------------

    @property
    def is_git(self) -> bool:
        """Whether writes land as git commits.

        The deploy repo always commits. A directory answers True when it sits
        INSIDE a git repository: scalo walks up, so a directory under a gitops
        checkout answers True without being a repository root itself.
        """
        if self._deploy is not None:
            return True
        return self._require_store().is_git

    @property
    def current_branch(self) -> str | None:
        """Current git branch name (directory backend only)."""
        return self._require_store().current_branch

    def list_branches(self) -> list[str]:
        """List all git branches (directory backend only)."""
        return self._require_store().list_branches()

    def switch_branch(self, branch: str, create: bool = False) -> None:
        """Switch to a git branch (directory backend only). Refreshes the cache after."""
        self._require_store().switch_branch(branch, create=create)

    def on_change(self, service: str, instance: str, callback) -> None:
        """Register a callback for when a service config changes (directory backend only).

        Args:
            service: Service name
            instance: Deployment instance name
            callback: Function called with (table_name, data) on change
        """
        table = self._table_name(service, instance)
        self._require_store().on_change(table, callback)

    # -------------------------------------------------------------------------
    # Seed Defaults
    # -------------------------------------------------------------------------

    def seed_defaults(
        self,
        overwrite: bool = False,
        *,
        created_by: str | None = None,
        routing: ReviewRouting | None = None,
    ) -> int:
        """Seed the configs with the built-in default configurations.

        Copies default YAML files from package resources. Non-destructive by
        default: skips configs that already exist. The deploy repo takes every
        seeded config in one commit.

        Args:
            overwrite: If True, overwrite existing configs with defaults.
            created_by: Username/identity, recorded on a deploy-repo commit.
            routing: Collects where a deploy-repo write landed.

        Returns:
            Number of configs seeded.
        """
        import importlib.resources as resources

        try:
            defaults_dir = resources.files("dfe_engine.services") / "default_configs"
            if not defaults_dir.is_dir():
                logger.warning("No default_configs package resource found")
                return 0
        except Exception as e:
            logger.warning(f"Failed to locate default configs: {e}")
            return 0

        if self._deploy is not None:
            return self._seed_deploy_repo(
                defaults_dir, overwrite=overwrite, created_by=created_by, routing=routing
            )

        count = 0
        for item in defaults_dir.iterdir():
            if not item.name.endswith(".yaml"):
                continue

            target = self._config_directory / item.name
            if target.exists() and not overwrite:
                logger.debug(f"Skipping existing config: {item.name}")
                continue

            # Read from package resource and write to config directory
            content = item.read_text(encoding="utf-8")
            target.write_text(content, encoding="utf-8")
            count += 1
            logger.info(f"Seeded default config: {item.name}")

        # Refresh cache to pick up new files
        if count > 0:
            self._require_store()._refresh_all()

        return count

    def _seed_deploy_repo(
        self,
        defaults_dir: Any,
        *,
        overwrite: bool,
        created_by: str | None,
        routing: ReviewRouting | None,
    ) -> int:
        """Commit each built-in default the deploy repo lacks, or every one on overwrite."""
        deploy = self._deploy
        if deploy is None:
            raise ServiceConfigError("no deploy repo backend")
        held = set(deploy.names())
        docs: dict[str, dict[str, Any]] = {}
        for item in sorted(defaults_dir.iterdir(), key=lambda entry: entry.name):
            if not item.name.endswith(".yaml"):
                continue
            table = item.name.removesuffix(".yaml")
            if table in held and not overwrite:
                logger.debug(f"Skipping existing config: {table}")
                continue
            doc = yaml_load_string(item.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                docs[table] = doc
        if not docs:
            return 0
        outcome = deploy.put_many(
            docs, actor=created_by or "engine", scope="services", summary="seed defaults"
        )
        if routing is not None:
            routing.record(outcome)
        logger.info(f"Seeded {len(docs)} default service configs into the deploy repo")
        return len(docs)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _validate_service(service: str) -> None:
        services = valid_services()
        if service not in services:
            msg = f"Unknown service: {service}. Valid: {', '.join(sorted(services))}"
            raise ValueError(msg)

    def close(self) -> None:
        """Stop background refresh and cleanup."""
        if self._store is not None:
            self._store.stop()
