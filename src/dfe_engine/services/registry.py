"""Service configuration registry backed by DirectoryConfigStore.

Storage model:
- YAML directory is the Single Source of Truth (SSoT)
- DirectoryConfigStore from scalo provides:
  - In-memory caching with background polling refresh
  - Thread-safe reads via RLock
  - Change callbacks for reactive configuration

Which services exist is the app manifest's (``appmgmt/apps.yaml``): every app whose
image reads a config file. What each one's config may hold, and what it runs when a
key is left out, is that app's own mounted contract (``appmgmt/contract.py``), so
no default or type is restated here.
"""

import copy
from dataclasses import replace
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from scalo.config import DirectoryConfigStore
from scalo.logger import logger

from dfe_engine.appmgmt import catalogue, contract
from dfe_engine.appmgmt.appconfig import CONFIG_ROOT
from dfe_engine.settings import DFESettings, get_settings
from dfe_engine.yaml_utils import deep_merge, yaml_dump, yaml_dump_string, yaml_load_string

RECEIVER_PRODUCTION = "receiver-production.yaml"
"""The shipped default the engine adds each bearer listener's auth to when it is seeded.

The listeners read the secret the engine writes their tokens to, so the block follows
the secrets backend the engine's settings name.
"""


def service_apps() -> dict[str, str]:
    """Every service the registry holds configs for, by name, with the app it configures.

    The manifest's apps whose image reads a config file, named without the
    ``dfe-`` prefix, as the stored files are: ``dfe-transform-vrl`` is
    ``transform-vrl``.
    """
    return {
        app.service.removeprefix(catalogue.PROJECT_PREFIX): app.service
        for app in catalogue.APP_CATALOGUE.values()
        if app.config_file
    }


def _with_bearer_auth(text: str, settings: DFESettings) -> str:
    """The receiver's production default with each bearer listener's auth block set."""
    from dfe_engine.orgs.ingest_credentials import BEARER_LISTENERS, LISTENER_SECTIONS
    from dfe_engine.orgs.ingest_tokens import bearer_auth

    config = yaml_load_string(text) or {}
    deep_merge(
        config,
        {
            LISTENER_SECTIONS[listener]: {"auth": bearer_auth(listener, settings.secrets)}
            for listener in BEARER_LISTENERS
        },
    )
    return yaml_dump_string(config)


def default_configs(settings: DFESettings | None = None) -> dict[str, str]:
    """Every config :meth:`ServiceConfigRegistry.seed_defaults` writes, by file name, as YAML.

    The files shipped under ``default_configs`` byte for byte, except
    :data:`RECEIVER_PRODUCTION`, which takes its bearer listeners from *settings*.

    Args:
        settings: The engine settings the receiver's bearer listeners are built from,
            the process's own when None.

    Raises:
        ValueError: The secrets backend is one dfe-receiver has no provider for.
    """
    shipped = resources.files("dfe_engine.services") / "default_configs"
    configs = {
        entry.name: entry.read_text(encoding="utf-8")
        for entry in shipped.iterdir()
        if entry.name.endswith(".yaml")
    }
    engine = settings if settings is not None else get_settings()
    configs[RECEIVER_PRODUCTION] = _with_bearer_auth(configs[RECEIVER_PRODUCTION], engine)
    return dict(sorted(configs.items()))


class ValidationResult(BaseModel):
    """Whether a service config is one its app accepts, and what was found."""

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ServiceConfigError(Exception):
    """Base exception for service config errors."""


class ConfigNotFoundError(ServiceConfigError):
    """Configuration not found for service/instance."""


class UnknownServiceError(ServiceConfigError):
    """The service names no app the manifest declares with a config file."""


class InvalidServiceConfigError(ServiceConfigError):
    """A config holds a value its app refuses at startup."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def _option_checks(
    options: dict[str, contract.DeclaredOption], value: Any, path: str
) -> tuple[list[str], list[str]]:
    """Each declared option under ``path`` judged, and each key no option explains."""
    option = options.get(f"{CONFIG_ROOT}.{path}")
    if option is not None:
        if value is None:
            return [], []
        refused = contract.check_value(replace(option, path=path), value)
        return ([refused] if refused else []), []
    if isinstance(value, dict):
        errors: list[str] = []
        warnings: list[str] = []
        for key, child in value.items():
            found, unknown = _option_checks(options, child, f"{path}.{key}" if path else str(key))
            errors += found
            warnings += unknown
        return errors, warnings
    return [], [f"{path} is not an option the app declares, so the app may ignore it"]


def judge_config(app_contract: contract.AppContract, config: dict) -> ValidationResult:
    """Whether *config* holds only values the app's own schema takes.

    A type, a closed set or a null the app refuses is an error. A key the schema
    does not declare is a warning, as is a config judged with no contract mounted.
    """
    if not app_contract.available:
        return ValidationResult(
            valid=True,
            warnings=[f"no config contract is mounted for {app_contract.service}; not checked"],
        )
    prefix = f"{CONFIG_ROOT}."
    nulls = contract.refused_nulls(app_contract, CONFIG_ROOT, config)
    errors = [f"{path.removeprefix(prefix)} does not accept a null" for path in nulls]
    found, warnings = _option_checks(contract.declared_options(app_contract), config, "")
    errors += found
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


class ServiceConfigRegistry:
    """Registry for managing DFE Rust service configurations.

    Backed by DirectoryConfigStore (YAML directory as SSoT).

    Directory layout:
        <config_directory>/
            receiver-default.yaml
            receiver-production.yaml
            loader-default.yaml
            ...

    Each file is named ``{service}-{instance}.yaml`` and holds what the operator
    set for that instance, nothing more: a key left out runs the app's own default.
    """

    _instance: ServiceConfigRegistry | None = None

    def __init__(
        self,
        config_directory: str | Path,
        writable: bool | None = None,
        refresh_interval: int = 30,
    ) -> None:
        """Initialize the registry.

        Args:
            config_directory: Path to the YAML config directory.
            writable: Whether writes are allowed. None = auto-detect.
            refresh_interval: Seconds between background cache refresh polls.
        """
        self._config_directory = Path(config_directory).resolve()
        self._config_directory.mkdir(parents=True, exist_ok=True)

        self._store = DirectoryConfigStore(
            directory=self._config_directory,
            refresh_interval=refresh_interval,
            writable=writable,
        )
        self._store.start()

    @classmethod
    def get_instance(
        cls,
        config_directory: str | Path | None = None,
        writable: bool | None = None,
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
    # Naming
    # -------------------------------------------------------------------------

    @staticmethod
    def _table_name(service: str, instance: str) -> str:
        """The DirectoryConfigStore table name: the YAML filename without extension."""
        return f"{service}-{instance}"

    @staticmethod
    def _parse_table_name(table: str) -> tuple[str, str] | None:
        """``(service, instance)`` for a table, or None for a file no service owns.

        The longest service name wins, so ``transform-vrl-x`` is ``transform-vrl``.
        """
        for svc in sorted(service_apps(), key=len, reverse=True):
            prefix = f"{svc}-"
            if table.startswith(prefix) and table[len(prefix) :]:
                return svc, table[len(prefix) :]
        return None

    @staticmethod
    def app_of(service: str) -> str:
        """The manifest app *service* configures.

        Raises:
            UnknownServiceError: No app in the manifest is configured by that name.
        """
        apps = service_apps()
        if service not in apps:
            known = ", ".join(sorted(apps))
            raise UnknownServiceError(f"Unknown service: {service}. Valid: {known}")
        return apps[service]

    def app_contract(self, service: str) -> contract.AppContract:
        """The contract the deployment mounted for *service*'s app.

        Raises:
            UnknownServiceError: As :meth:`app_of`.
            ContractError: The mounted contract is there but unreadable.
        """
        return contract.contract(self.app_of(service))

    # -------------------------------------------------------------------------
    # CRUD Operations
    # -------------------------------------------------------------------------

    def get_config(self, service: str, instance: str = "default") -> dict[str, Any]:
        """The config stored for *service*/*instance*, as its file holds it.

        Raises:
            UnknownServiceError: As :meth:`app_of`.
            ConfigNotFoundError: Nothing is stored for the instance.
        """
        self.app_of(service)
        config_data = self._store.get(self._table_name(service, instance))
        if config_data is None:
            raise ConfigNotFoundError(f"Config not found for {service}/{instance}")
        return copy.deepcopy(config_data)

    def defaults(self, service: str) -> dict[str, Any]:
        """What *service*'s app runs for every key its file leaves out, empty with no contract.

        Raises:
            UnknownServiceError: As :meth:`app_of`.
            ContractError: As :meth:`app_contract`.
        """
        found = self.app_contract(service)
        return contract.config_defaults(found.schema) if found.available else {}

    def effective_config(self, service: str, instance: str = "default") -> dict[str, Any]:
        """The stored config over the app's own defaults: what the instance runs.

        Raises:
            UnknownServiceError: As :meth:`app_of`.
            ConfigNotFoundError: As :meth:`get_config`.
            ContractError: As :meth:`app_contract`.
        """
        stored = self.get_config(service, instance)
        return deep_merge(self.defaults(service), stored)

    def save_config(self, service: str, config: dict[str, Any], instance: str = "default") -> None:
        """Write *config* as the instance's file, after the app's contract has judged it.

        Raises:
            UnknownServiceError: As :meth:`app_of`.
            InvalidServiceConfigError: The config holds a value the app refuses.
            ContractError: As :meth:`app_contract`.
        """
        result = judge_config(self.app_contract(service), config)
        if not result.valid:
            raise InvalidServiceConfigError(result.errors)

        table = self._table_name(service, instance)
        yaml_path = self._config_directory / f"{table}.yaml"
        yaml_dump(config, yaml_path)

        # Force cache refresh for this table
        self._store._refresh_all()

        logger.info(f"Saved config for {service}/{instance} -> {yaml_path}")

    def delete_config(self, service: str, instance: str = "default") -> None:
        """Delete a service configuration.

        Raises:
            UnknownServiceError: As :meth:`app_of`.
        """
        self.app_of(service)
        table = self._table_name(service, instance)
        yaml_path = self._config_directory / f"{table}.yaml"

        if not yaml_path.exists():
            logger.warning(f"Config file does not exist: {yaml_path}")
            return

        yaml_path.unlink(missing_ok=True)

        with self._store._lock:
            self._store._cache.pop(table, None)

        logger.info(f"Deleted config for {service}/{instance}")

    def list_configs(self, service: str | None = None) -> list[dict[str, Any]]:
        """List every stored configuration a known service owns.

        Args:
            service: Optional filter by service name

        Returns:
            List of config metadata dicts (service, instance, updated_at)
        """
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
        """Judge a configuration against its app's contract without saving it.

        Raises:
            ContractError: As :meth:`app_contract`.
        """
        try:
            found = self.app_contract(service)
        except UnknownServiceError as exc:
            return ValidationResult(valid=False, errors=[str(exc)])
        return judge_config(found, config_data)

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
    # Seed Defaults
    # -------------------------------------------------------------------------

    def seed_defaults(self, overwrite: bool = False, *, settings: DFESettings | None = None) -> int:
        """Seed the config directory with built-in default configurations.

        Writes each of :func:`default_configs`. Non-destructive by default: skips
        files that already exist.

        Args:
            overwrite: If True, overwrite existing configs with defaults.
            settings: The engine settings the receiver's production default is built
                from, the process's own when None.

        Returns:
            Number of configs seeded.

        Raises:
            ValueError: As :func:`default_configs`.
        """
        count = 0
        for name, content in default_configs(settings).items():
            target = self._config_directory / name
            if target.exists() and not overwrite:
                logger.debug(f"Skipping existing config: {name}")
                continue
            target.write_text(content, encoding="utf-8")
            count += 1
            logger.info(f"Seeded default config: {name}")

        # Refresh cache to pick up new files
        if count > 0:
            self._store._refresh_all()

        return count

    def close(self) -> None:
        """Stop background refresh and cleanup."""
        self._store.stop()
