"""Deployment configuration registry backed by DirectoryConfigStore.

Storage model:
- YAML directory is the Single Source of Truth (SSoT)
- DirectoryConfigStore from scalo provides:
  - In-memory caching with background polling refresh
  - Thread-safe reads via RLock
  - Optional git-aware writes (auto-commit, branch management, push)
  - Change callbacks for reactive configuration
- Deployment configs live in a ``deploy/`` subdirectory alongside service configs
- Config history tracked via git log (when directory is a git repo)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from scalo.config import DirectoryConfigStore
from scalo.logger import logger

from dfe_engine.deployment.sizing import apply_sizing
from dfe_engine.deployment.validators import ValidationResult, validate_deployment_config
from dfe_engine.git_identity import (
    COMMITTER_IDENTITY,
    commit_file,
    git_repo_relative_path,
)
from dfe_engine.services.plugins import deployment_classes, valid_services
from dfe_engine.yaml_utils import yaml_dump


class DeploymentConfigError(Exception):
    """Base exception for deployment config errors."""


class DeploymentConfigNotFoundError(DeploymentConfigError):
    """Configuration not found for service/instance."""


class DeploymentConfigRegistry:
    """Registry for managing DFE deployment configurations.

    Backed by DirectoryConfigStore (YAML directory as SSoT).

    Directory layout:
        <config_directory>/
            receiver-default.yaml
            receiver-production.yaml
            loader-default.yaml
            loader-production.yaml
            archiver-default.yaml
            archiver-production.yaml

    Each file is named ``{service}-{instance}.yaml`` and contains the
    deployment configuration for that service instance.
    """

    _instance: DeploymentConfigRegistry | None = None

    def __init__(
        self,
        config_directory: str | Path,
        writable: bool | None = None,
        git_branch: str | None = None,
        git_push: bool = False,
        refresh_interval: int = 30,
    ) -> None:
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

    @classmethod
    def get_instance(
        cls,
        config_directory: str | Path | None = None,
        writable: bool | None = None,
        git_branch: str | None = None,
        git_push: bool = False,
        refresh_interval: int = 30,
    ) -> DeploymentConfigRegistry:
        """Get singleton registry instance."""
        if cls._instance is None:
            if config_directory is None:
                raise DeploymentConfigError(
                    "config_directory is required on first call to get_instance()"
                )
            cls._instance = DeploymentConfigRegistry(
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
        return f"{service}-{instance}"

    @staticmethod
    def _parse_table_name(table: str) -> tuple[str, str] | None:
        for svc in sorted(valid_services(), key=len, reverse=True):
            prefix = f"{svc}-"
            if table.startswith(prefix):
                instance = table[len(prefix) :]
                if instance:
                    return svc, instance
        # Unknown service: split on last hyphen (schema-less fallback)
        idx = table.rfind("-")
        if idx > 0 and idx < len(table) - 1:
            return table[:idx], table[idx + 1 :]
        return None

    # -------------------------------------------------------------------------
    # CRUD Operations
    # -------------------------------------------------------------------------

    def get_config(self, service: str, instance: str = "default"):
        """Get a deployment configuration.

        Returns a typed model for registered services, raw dict for unknown ones.
        """
        table = self._table_name(service, instance)

        config_data = self._store.get(table)
        if config_data is None:
            raise DeploymentConfigNotFoundError(
                f"Deployment config not found for {service}/{instance}"
            )

        classes = deployment_classes()
        if service in classes:
            return classes[service].model_validate(config_data)
        return config_data

    def save_config(
        self,
        service: str,
        config,
        instance: str = "default",
        created_by: str | None = None,
        description: str | None = None,
    ) -> None:
        """Save a deployment configuration to the YAML directory."""
        if isinstance(config, dict):
            classes = deployment_classes()
            if service in classes:
                validated = classes[service].model_validate(config)
                config_data = validated.model_dump(mode="json")
            else:
                # Unknown service — store raw dict (schema-less mode)
                config_data = config
        else:
            config_data = config.model_dump(mode="json")

        table = self._table_name(service, instance)
        yaml_path = self._config_directory / f"{table}.yaml"
        yaml_dump(config_data, yaml_path)

        if self._store.is_git:
            commit_msg = description or f"deploy: update {service}/{instance}"
            if created_by:
                commit_msg = f"{commit_msg} (by {created_by})"
            commit_file(self._store, yaml_path, commit_msg, author=created_by)
            if self._store._git_push:
                self._store._git_push_remote()

        self._store._refresh_all()
        logger.info(f"Saved deployment config for {service}/{instance} → {yaml_path}")

    def delete_config(self, service: str, instance: str = "default") -> None:
        """Delete a deployment configuration."""
        table = self._table_name(service, instance)
        yaml_path = self._config_directory / f"{table}.yaml"

        if not yaml_path.exists():
            logger.warning(f"Deployment config file does not exist: {yaml_path}")
            return

        resolved_path = yaml_path.resolve(strict=False)
        resolved_path.unlink(missing_ok=True)

        if self._store.is_git and self._store._repo is not None:
            try:
                from dulwich import porcelain as git

                rel_path = git_repo_relative_path(self._store._repo.path, resolved_path)
                git.rm(self._store._repo, paths=[rel_path])
                git.commit(
                    self._store._repo,
                    author=COMMITTER_IDENTITY.encode("utf-8"),
                    committer=COMMITTER_IDENTITY.encode("utf-8"),
                    message=f"deploy: delete {service}/{instance}".encode(),
                )
                if self._store._git_push:
                    self._store._git_push_remote()
            except Exception as e:
                logger.error(f"Git delete failed: {e}")

        with self._store._lock:
            self._store._cache.pop(table, None)

        logger.info(f"Deleted deployment config for {service}/{instance}")

    def list_configs(self, service: str | None = None) -> list[dict[str, Any]]:
        """List all stored deployment configurations."""
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
        """Validate a deployment configuration without saving (dry-run)."""
        return validate_deployment_config(service, config_data)

    # -------------------------------------------------------------------------
    # History (git log)
    # -------------------------------------------------------------------------

    def get_config_history(
        self, service: str, instance: str = "default", limit: int = 10
    ) -> list[dict[str, Any]]:
        """Get deployment config change history from git log."""
        self._validate_service(service)

        if not self._store.is_git or self._store._repo is None:
            logger.warning("Config history requires a git-backed config directory")
            return []

        table = self._table_name(service, instance)
        yaml_file = f"{table}.yaml"
        repo = self._store._repo

        try:
            from dulwich.objects import Commit, Tree
            from dulwich.walk import Walker

            rel_path = yaml_file.encode("utf-8")
            history = []
            walker = Walker(repo.object_store, [repo.head()])

            for entry in walker:
                if len(history) >= limit:
                    break

                commit = entry.commit
                tree = cast("Tree", repo[commit.tree])
                try:
                    tree.lookup_path(repo.__getitem__, rel_path)
                except KeyError:
                    continue

                if commit.parents:
                    parent = cast("Commit", repo[commit.parents[0]])
                    parent_tree = cast("Tree", repo[parent.tree])
                    try:
                        parent_entry = parent_tree.lookup_path(repo.__getitem__, rel_path)
                        current_entry = tree.lookup_path(repo.__getitem__, rel_path)
                        if parent_entry[1] == current_entry[1]:
                            continue
                    except KeyError:
                        pass

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

            return history

        except Exception as e:
            logger.warning(f"Failed to read git history for {yaml_file}: {e}")
            return []

    # -------------------------------------------------------------------------
    # Sizing Operations
    # -------------------------------------------------------------------------

    def apply_size(
        self,
        service: str,
        instance: str,
        size: str,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """Apply t-shirt sizing to a deployment config and save it.

        Updates the deployment config with the new size and corresponding
        resource spec. Returns the service config overrides that the caller
        can optionally apply to the ServiceConfigRegistry.

        Args:
            service: Service name
            instance: Deployment instance name
            size: T-shirt size (xs, small, medium, large, xlarge)
            created_by: Username/identity

        Returns:
            Service config overrides dict (matching service config structure)
        """
        deploy_overrides, service_overrides = apply_sizing(service, size)

        # Load existing config or create from defaults
        try:
            config = self.get_config(service, instance)
            config_data = config.model_dump(mode="json")
        except DeploymentConfigNotFoundError:
            config_cls = deployment_classes()[service]
            config_data = config_cls().model_dump(mode="json")

        # Apply sizing
        config_data["size"] = size
        config_data["resources"] = deploy_overrides["resources"]

        # Update KEDA defaults if present
        if "keda" in deploy_overrides:
            for k, v in deploy_overrides["keda"].items():
                config_data["keda"][k] = v

        self.save_config(
            service,
            config_data,
            instance=instance,
            created_by=created_by,
            description=f"deploy: resize {service}/{instance} to {size}",
        )

        return service_overrides

    # -------------------------------------------------------------------------
    # Helm Values Export
    # -------------------------------------------------------------------------

    def export_helm_values(self, service: str, instance: str, path: Path) -> Path:
        """Export a deployment config as Helm values file.

        Strips the ``size`` field (Helm doesn't need it — resources are
        already expanded) and writes clean YAML.

        Args:
            service: Service name
            instance: Deployment instance name
            path: Output file path

        Returns:
            Path to the written file
        """
        config = self.get_config(service, instance)
        config_data = config.model_dump(mode="json")

        # Strip size — Helm uses the expanded resources directly
        config_data.pop("size", None)

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        yaml_dump(config_data, path)
        logger.info(f"Exported Helm values for {service}/{instance} to {path}")
        return path

    # -------------------------------------------------------------------------
    # Seed Defaults
    # -------------------------------------------------------------------------

    def seed_defaults(self, overwrite: bool = False) -> int:
        """Seed the config directory with built-in default deployment configs."""
        import importlib.resources as resources

        try:
            defaults_dir = resources.files("dfe_engine.deployment") / "default_configs"
            if not defaults_dir.is_dir():
                logger.warning("No default_configs package resource found")
                return 0
        except Exception as e:
            logger.warning(f"Failed to locate default configs: {e}")
            return 0

        count = 0
        for item in defaults_dir.iterdir():
            if not item.name.endswith(".yaml"):
                continue

            target = self._config_directory / item.name
            if target.exists() and not overwrite:
                logger.debug(f"Skipping existing config: {item.name}")
                continue

            content = item.read_text(encoding="utf-8")
            target.write_text(content, encoding="utf-8")
            count += 1
            logger.info(f"Seeded default deployment config: {item.name}")

        if count > 0:
            self._store._refresh_all()

        return count

    # -------------------------------------------------------------------------
    # Git Operations (passthrough)
    # -------------------------------------------------------------------------

    @property
    def is_git(self) -> bool:
        return self._store.is_git

    @property
    def current_branch(self) -> str | None:
        return self._store.current_branch

    def list_branches(self) -> list[str]:
        return self._store.list_branches()

    def switch_branch(self, branch: str, create: bool = False) -> None:
        self._store.switch_branch(branch, create=create)

    def on_change(self, service: str, instance: str, callback) -> None:
        table = self._table_name(service, instance)
        self._store.on_change(table, callback)

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
        self._store.stop()
