"""Service configuration registry backed by DirectoryConfigStore.

Storage model:
- YAML directory is the Single Source of Truth (SSoT)
- DirectoryConfigStore from hyperi-pylib provides:
  - In-memory caching with background polling refresh
  - Thread-safe reads via RLock
  - Optional git-aware writes (auto-commit, branch management, push)
  - Change callbacks for reactive configuration
- Rust services (loader, receiver, archiver) read these YAML files directly
- Config history is tracked via git log (when directory is a git repo)

This replaces the previous PostgreSQL + YAML dual-write architecture.
PostgreSQL is no longer required for service configuration storage.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hyperi_pylib.config import DirectoryConfigStore
from hyperi_pylib.logger import logger

from dfe_engine.services.models import (
    SERVICE_CONFIG_CLASSES,
    VALID_SERVICES,
    ArchiverConfig,
    LoaderConfig,
    ReceiverConfig,
)
from dfe_engine.services.validators import ValidationResult, validate_config
from dfe_engine.yaml_utils import yaml_dump


class ServiceConfigError(Exception):
    """Base exception for service config errors."""


class ConfigNotFoundError(ServiceConfigError):
    """Configuration not found for service/instance."""


class ServiceConfigRegistry:
    """Registry for managing DFE Rust service configurations.

    Backed by DirectoryConfigStore (YAML directory as SSoT).

    Directory layout:
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
        config_directory: str | Path,
        writable: bool | None = None,
        git_branch: str | None = None,
        git_push: bool = False,
        refresh_interval: int = 30,
    ) -> None:
        """Initialize the registry.

        Args:
            config_directory: Path to the YAML config directory.
            writable: Whether writes are allowed. None = auto-detect.
            git_branch: Git branch for writes. None = current branch.
            git_push: Auto-push after git commits.
            refresh_interval: Seconds between background cache refresh polls.
        """
        self._config_directory = Path(config_directory)
        self._config_directory.mkdir(parents=True, exist_ok=True)

        self._store = DirectoryConfigStore(
            directory=self._config_directory,
            refresh_interval=refresh_interval,
            writable=writable,
            git_branch=git_branch,
            git_push=git_push,
        )
        self._store.start()

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

        Returns None if the table name doesn't match a known service.
        """
        for svc in sorted(VALID_SERVICES, key=len, reverse=True):
            prefix = f"{svc}-"
            if table.startswith(prefix):
                instance = table[len(prefix):]
                if instance:
                    return svc, instance
        return None

    # -------------------------------------------------------------------------
    # CRUD Operations
    # -------------------------------------------------------------------------

    def get_config(
        self, service: str, instance: str = "default"
    ) -> ReceiverConfig | LoaderConfig | ArchiverConfig:
        """Get a service configuration.

        Reads from the DirectoryConfigStore in-memory cache (backed by YAML).

        Args:
            service: Service name ('receiver', 'loader', 'archiver')
            instance: Deployment instance name (e.g., 'default', 'production')

        Returns:
            Typed configuration model

        Raises:
            ConfigNotFoundError: Config not found
        """
        self._validate_service(service)
        config_cls = SERVICE_CONFIG_CLASSES[service]
        table = self._table_name(service, instance)

        config_data = self._store.get(table)
        if config_data is None:
            raise ConfigNotFoundError(
                f"Config not found for {service}/{instance}"
            )

        return config_cls.model_validate(config_data)

    def save_config(
        self,
        service: str,
        config: ReceiverConfig | LoaderConfig | ArchiverConfig | dict,
        instance: str = "default",
        created_by: str | None = None,
        description: str | None = None,
    ) -> None:
        """Save a service configuration to the YAML directory.

        If the directory is a git repo, changes are auto-committed.

        Args:
            service: Service name
            config: Configuration model or dict
            instance: Deployment instance name
            created_by: Username/identity of who made the change
            description: Description of the change
        """
        self._validate_service(service)

        # Normalize to dict via Pydantic validation
        if isinstance(config, dict):
            config_cls = SERVICE_CONFIG_CLASSES[service]
            validated = config_cls.model_validate(config)
            config_data = validated.model_dump(mode="json")
        else:
            config_data = config.model_dump(mode="json")

        table = self._table_name(service, instance)

        # Write YAML file directly (full document replacement)
        yaml_path = self._config_directory / f"{table}.yaml"
        yaml_dump(config_data, yaml_path)

        # Git commit if the store is git-aware
        if self._store.is_git:
            commit_msg = description or f"config: update {service}/{instance}"
            if created_by:
                commit_msg = f"{commit_msg} (by {created_by})"
            self._store._git_commit(yaml_path, commit_msg, author=created_by)
            if self._store._git_push:
                self._store._git_push_remote()

        # Force cache refresh for this table
        self._store._refresh_all()

        logger.info(f"Saved config for {service}/{instance} → {yaml_path}")

    def delete_config(self, service: str, instance: str = "default") -> None:
        """Delete a service configuration.

        Removes the YAML file and commits the deletion if git-aware.

        Args:
            service: Service name
            instance: Deployment instance name
        """
        self._validate_service(service)
        table = self._table_name(service, instance)
        yaml_path = self._config_directory / f"{table}.yaml"

        if not yaml_path.exists():
            logger.warning(f"Config file does not exist: {yaml_path}")
            return

        # Git rm + commit if git-aware
        if self._store.is_git and self._store._repo is not None:
            try:
                from dulwich import porcelain as git

                repo_root = Path(self._store._repo.path)
                rel_path = str(yaml_path.relative_to(repo_root))

                # Remove file from disk and stage removal
                yaml_path.unlink()
                git.rm(self._store._repo, paths=[rel_path])
                git.commit(
                    self._store._repo,
                    message=f"config: delete {service}/{instance}".encode("utf-8"),
                )
                if self._store._git_push:
                    self._store._git_push_remote()
            except Exception as e:
                logger.error(f"Git delete failed: {e}")
                # File already unlinked above, that's OK
        else:
            yaml_path.unlink()

        # Remove from cache
        with self._store._lock:
            self._store._cache.pop(table, None)

        logger.info(f"Deleted config for {service}/{instance}")

    def list_configs(self, service: str | None = None) -> list[dict[str, Any]]:
        """List all stored configurations.

        Args:
            service: Optional filter by service name

        Returns:
            List of config metadata dicts (service, instance, updated_at)
        """
        if service:
            self._validate_service(service)

        results = []
        for table in self._store.list_tables():
            parsed = self._parse_table_name(table)
            if parsed is None:
                continue
            svc, inst = parsed
            if service and svc != service:
                continue

            yaml_path = self._config_directory / f"{table}.yaml"
            try:
                stat = yaml_path.stat()
                updated_at = datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat()
            except OSError:
                updated_at = None

            results.append({
                "service": svc,
                "instance": inst,
                "updated_at": updated_at,
            })

        return results

    # -------------------------------------------------------------------------
    # Validation
    # -------------------------------------------------------------------------

    def validate(self, service: str, config_data: dict) -> ValidationResult:
        """Validate a configuration without saving (dry-run).

        Args:
            service: Service name
            config_data: Configuration dictionary

        Returns:
            ValidationResult with errors and warnings
        """
        return validate_config(service, config_data)

    # -------------------------------------------------------------------------
    # History (git log)
    # -------------------------------------------------------------------------

    def get_config_history(
        self, service: str, instance: str = "default", limit: int = 10
    ) -> list[dict[str, Any]]:
        """Get configuration change history from git log.

        Only available when the config directory is a git repo.
        Uses dulwich to walk the commit history.

        Args:
            service: Service name
            instance: Deployment instance name
            limit: Maximum number of history entries

        Returns:
            List of history entries (commit, message, author, date)
        """
        self._validate_service(service)

        if not self._store.is_git or self._store._repo is None:
            logger.warning("Config history requires a git-backed config directory")
            return []

        table = self._table_name(service, instance)
        yaml_file = f"{table}.yaml"
        repo = self._store._repo

        try:
            from dulwich.walk import Walker

            rel_path = yaml_file.encode("utf-8")

            history = []
            walker = Walker(repo.object_store, [repo.head()])

            for entry in walker:
                if len(history) >= limit:
                    break

                commit = entry.commit
                # Check if this commit touches our file
                tree = repo[commit.tree]
                try:
                    tree.lookup_path(repo.__getitem__, rel_path)
                except KeyError:
                    continue

                # Check if file changed vs parent
                if commit.parents:
                    parent = repo[commit.parents[0]]
                    parent_tree = repo[parent.tree]
                    try:
                        parent_entry = parent_tree.lookup_path(
                            repo.__getitem__, rel_path
                        )
                        current_entry = tree.lookup_path(
                            repo.__getitem__, rel_path
                        )
                        if parent_entry[1] == current_entry[1]:
                            continue  # File unchanged in this commit
                    except KeyError:
                        pass  # File was added in this commit

                author_str = commit.author.decode("utf-8", errors="replace")
                # Parse "Name <email>" format — extract just the name
                author_name = author_str.split("<")[0].strip() if "<" in author_str else author_str

                history.append({
                    "commit": commit.id.decode("ascii"),
                    "message": commit.message.decode("utf-8", errors="replace").strip(),
                    "author": author_name,
                    "date": datetime.fromtimestamp(
                        commit.author_time, tz=timezone.utc
                    ).isoformat(),
                })

            return history

        except Exception as e:
            logger.warning(f"Failed to read git history for {yaml_file}: {e}")
            return []

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
        config = self.get_config(service, instance)
        config_data = config.model_dump(mode="json")
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
        """Whether the config directory is a git repository."""
        return self._store.is_git

    @property
    def current_branch(self) -> str | None:
        """Current git branch name."""
        return self._store.current_branch

    def list_branches(self) -> list[str]:
        """List all git branches."""
        return self._store.list_branches()

    def switch_branch(self, branch: str, create: bool = False) -> None:
        """Switch to a git branch. Refreshes config cache after switch."""
        self._store.switch_branch(branch, create=create)

    def on_change(self, service: str, instance: str, callback) -> None:
        """Register a callback for when a service config changes.

        Args:
            service: Service name
            instance: Deployment instance name
            callback: Function called with (table_name, data) on change
        """
        table = self._table_name(service, instance)
        self._store.on_change(table, callback)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _validate_service(service: str) -> None:
        if service not in VALID_SERVICES:
            msg = f"Unknown service: {service}. Valid: {', '.join(sorted(VALID_SERVICES))}"
            raise ValueError(msg)

    def close(self) -> None:
        """Stop background refresh and cleanup."""
        self._store.stop()
