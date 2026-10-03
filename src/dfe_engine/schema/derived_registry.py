#  Project:      dfe-engine
#  File:         schema/derived_registry.py
#  Purpose:      CRUD store for derived schemas (gitcrud, or a plain YAML tree)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Where a deployment's derived schemas live.

Two interchangeable backends behind one surface, the same pair ``SourceRegistry``
carries:

- **gitcrud** (preferred): the document IS the gitcrud doc in the deploy repo's
  ``config/schemas/derived/`` (ResourceClass ``derived_schemas``). Every mutation
  is one git commit, so a derived schema versions and audits beside the sources
  that name it.
- **directory** (standalone): ``<schemas_dir>/derived/`` as a plain YAML tree,
  used when no gitops deploy repo is configured.

A registry key is ``<group>/<name>`` or a bare ``<name>``; the reference a source
carries adds the ``derived/`` segment in front, and both backends resolve that
reference under :meth:`DerivedSchemaRegistry.reference_root`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scalo.logger import logger

from dfe_engine.schema.derived import DERIVED_PREFIX, DerivedSchema
from dfe_engine.schema.schema_loader import resolve_schema_yaml_path
from dfe_engine.yaml_utils import yaml_dump, yaml_load

if TYPE_CHECKING:
    from dfe_engine.gitcrud.engine import GitCrud

DERIVED_CLASS = "derived_schemas"


class DerivedSchemaError(Exception):
    """Base exception for the derived-schema store."""


class DerivedSchemaNotFoundError(DerivedSchemaError):
    """No derived schema at that registry path."""


class DerivedSchemaValidationError(DerivedSchemaError):
    """The document or its registry path failed validation."""


def canonical_derived_path(path: str) -> str:
    """Normalise a derived registry key to ``<group>/<name>``, without the prefix.

    Accepts the reference form the UI sends (``derived/beats/filebeat_auth``) and
    the bare key, so one string identifies the resource wherever it comes from.
    """
    normalised = str(path).replace("\\", "/").strip("/")
    parts = [part for part in normalised.split("/") if part]
    if parts and parts[0] == DERIVED_PREFIX:
        parts = parts[1:]
    if not parts:
        raise DerivedSchemaValidationError("Invalid empty derived-schema path")
    for segment in parts:
        if segment in (".", ".."):
            raise DerivedSchemaValidationError(f"Invalid path segment: {segment!r}")
        if "\x00" in segment:
            raise DerivedSchemaValidationError("Invalid path: null byte in segment")
    return "/".join(parts)


def derived_reference(path: str) -> str:
    """The reference a source stores for a derived schema: ``derived/<group>/<name>``."""
    return f"{DERIVED_PREFIX}/{canonical_derived_path(path)}"


def _class_directory() -> str:
    """The deploy-repo directory the ``derived_schemas`` class declares."""
    from dfe_engine.gitcrud.registry import default_registry

    return default_registry().get(DERIVED_CLASS).directory


def derived_reference_root(settings: Any) -> Path | None:
    """Root a ``derived/<group>/<name>`` reference resolves under, for this deployment.

    Gitops on puts the documents in the deploy repo, so the root is the class
    directory's parent; otherwise they sit under the runtime schemas tree. None
    when the deployment configures neither, which leaves the reference to be
    resolved as it was given.
    """
    gitops = getattr(settings, "gitops", None)
    if gitops is not None and gitops.enabled and gitops.local_path:
        return Path(gitops.local_path) / Path(_class_directory()).parent
    return Path(settings.schemas.schemas_dir) if settings.schemas.schemas_dir else None


def resolve_derived_reference(reference: str, *roots: Path | None) -> Path:
    """The file a ``derived/...`` reference names, from the first root that has one.

    Callers pass this deployment's store first and the shipped schemas tree
    after it. With gitops on the store is the deploy repo, and nothing copies a
    release's derived schemas into it, so a reference only the release carries
    resolves in the shipped tree. A miss returns the first root's candidate, so
    the error names where the deployment's own copy belongs.
    """
    candidates = [resolve_schema_yaml_path(root, reference) for root in roots if root is not None]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0] if candidates else Path(reference)


def derived_directory(settings: Any) -> Path:
    """Where this deployment's derived-schema documents sit.

    Raises:
        DerivedSchemaError: The deployment configures no store for them.
    """
    root = derived_reference_root(settings)
    if root is None:
        raise DerivedSchemaError(
            "no derived-schema store: set schemas.schemas_dir or enable gitops"
        )
    return root / DERIVED_PREFIX


class DerivedSchemaRegistry:
    """CRUD over derived-schema documents."""

    def __init__(
        self,
        derived_directory: str | Path | None = None,
        *,
        crud: GitCrud | None = None,
    ) -> None:
        """Bind the store to a backend.

        Args:
            derived_directory: ``<schemas_dir>/derived`` for the directory
                backend. Ignored when ``crud`` is given.
            crud: Governed Ops engine over the deploy repo. When provided,
                ``config/schemas/derived/`` is the SSoT and every mutation is
                one git commit.
        """
        self._crud = crud
        if crud is not None:
            cls = crud.resource_class(DERIVED_CLASS)
            self._directory = crud.repo_path / cls.directory
            return
        if derived_directory is None:
            raise DerivedSchemaError("derived_directory is required without a gitcrud backend")
        self._directory = Path(derived_directory).expanduser().resolve(strict=False)
        self._directory.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_settings(cls, settings: Any, *, crud: GitCrud | None = None) -> DerivedSchemaRegistry:
        """Bind to the deploy repo when gitops is on, else to the schemas tree."""
        if crud is not None:
            return cls(crud=crud)
        return cls(derived_directory(settings))

    @property
    def directory(self) -> Path:
        """Where the documents themselves sit."""
        return self._directory

    @property
    def reference_root(self) -> Path:
        """The root a ``derived/<group>/<name>`` reference resolves under.

        One level above the documents, so the ``derived/`` segment a source
        carries lands on this class's directory whichever backend is active.
        """
        return self._directory.parent

    @property
    def is_git(self) -> bool:
        """Whether writes land as git commits."""
        return self._crud is not None

    def _yaml_path(self, key: str) -> Path:
        # Append rather than with_suffix: a name carrying a dot keeps it.
        segments = key.split("/")
        candidate = self._directory.joinpath(*segments[:-1]) / f"{segments[-1]}.yaml"
        base = self._directory.resolve(strict=False)
        if not candidate.resolve(strict=False).is_relative_to(base):
            raise DerivedSchemaValidationError("Derived schema path escapes its directory")
        return candidate

    # -----------------------------------------------------------------
    # Backend primitives
    # -----------------------------------------------------------------

    def _names(self) -> list[str]:
        if self._crud is not None:
            return self._crud.list(DERIVED_CLASS)
        return sorted(
            p.relative_to(self._directory).as_posix()[: -len(".yaml")]
            for p in self._directory.glob("**/*.yaml")
            if p.is_file()
        )

    def _get_raw(self, key: str) -> dict[str, Any] | None:
        if self._crud is not None:
            from dfe_engine.gitcrud.engine import ResourceNotFoundError

            try:
                return self._crud.get(DERIVED_CLASS, key)
            except ResourceNotFoundError:
                return None
        path = self._yaml_path(key)
        if not path.is_file():
            return None
        return yaml_load(path) or {}

    def _put_raw(
        self,
        key: str,
        doc: dict[str, Any],
        *,
        created_by: str | None,
        message: str,
        summary: str = "",
    ):
        """Write one document; returns the destination label for the save log."""
        if self._crud is not None:
            from dfe_engine.gitcrud.metadata import ResourceMetadata, with_metadata

            stored = with_metadata(doc, ResourceMetadata(description=summary))
            self._crud.put(
                DERIVED_CLASS,
                key,
                stored,
                actor=created_by or "engine",
                message=message,
            )
            return f"gitcrud {self._crud.resource_class(DERIVED_CLASS).directory}"

        path = self._yaml_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        yaml_dump(doc, path)
        return str(path)

    def _delete_raw(self, key: str, *, created_by: str | None, message: str) -> bool:
        if self._crud is not None:
            from dfe_engine.gitcrud.engine import ResourceNotFoundError

            commit_msg = f"{message} (by {created_by})" if created_by else message
            try:
                self._crud.delete(
                    DERIVED_CLASS, key, actor=created_by or "engine", message=commit_msg
                )
            except ResourceNotFoundError:
                return False
            return True

        path = self._yaml_path(key)
        if not path.is_file():
            return False
        path.unlink()
        return True

    # -----------------------------------------------------------------
    # CRUD
    # -----------------------------------------------------------------

    def exists(self, path: str) -> bool:
        """Whether a derived schema is stored at that registry path."""
        return self._get_raw(canonical_derived_path(path)) is not None

    def get(self, path: str) -> DerivedSchema:
        """Read one derived schema.

        Raises:
            DerivedSchemaNotFoundError: Nothing stored at that path.
            DerivedSchemaValidationError: The stored document no longer parses.
        """
        key = canonical_derived_path(path)
        raw = self._get_raw(key)
        if raw is None:
            raise DerivedSchemaNotFoundError(f"Derived schema not found: {key!r}")
        try:
            schema = DerivedSchema.model_validate(raw)
        except Exception as exc:
            raise DerivedSchemaValidationError(f"Invalid derived schema {key!r}: {exc}") from exc
        schema.path = derived_reference(key)
        return schema

    def save(
        self,
        schema: DerivedSchema,
        *,
        created_by: str | None = None,
        description: str | None = None,
    ) -> DerivedSchema:
        """Write one derived schema, replacing whatever is stored at its path."""
        if not schema.path:
            raise DerivedSchemaValidationError("DerivedSchema.path is required when saving")
        key = canonical_derived_path(schema.path)
        message = description or f"schema: update derived/{key}"
        if created_by:
            message = f"{message} (by {created_by})"
        dest = self._put_raw(
            key,
            schema.to_yaml_dict(),
            created_by=created_by,
            message=message,
            summary=schema.version().summary,
        )
        logger.info(f"Saved derived schema {key!r} -> {dest}")
        return schema.model_copy(update={"path": derived_reference(key)})

    def delete(self, path: str, *, created_by: str | None = None) -> None:
        """Remove one derived schema.

        Raises:
            DerivedSchemaNotFoundError: Nothing stored at that path.
        """
        key = canonical_derived_path(path)
        if not self._delete_raw(
            key, created_by=created_by, message=f"schema: delete derived/{key}"
        ):
            raise DerivedSchemaNotFoundError(f"Derived schema not found: {key!r}")
        logger.info(f"Deleted derived schema {key!r}")

    def list(self) -> list[dict[str, Any]]:
        """Every stored derived schema, as list rows.

        A document that no longer parses is left out rather than failing the
        listing, the way the meta-schema and source listings do.
        """
        rows: list[dict[str, Any]] = []
        for key in self._names():
            raw = self._get_raw(key)
            if raw is None:
                continue
            try:
                schema = DerivedSchema.model_validate(raw)
            except Exception as exc:
                logger.warning(f"Failed to parse derived schema {key!r}, excluded from list: {exc}")
                continue
            try:
                updated_at = datetime.fromtimestamp(
                    self._yaml_path(key).stat().st_mtime, tz=UTC
                ).isoformat()
            except OSError:
                updated_at = ""
            rows.append(
                {
                    "path": derived_reference(key),
                    "base": schema.base,
                    "base_version": schema.base_version,
                    "current": schema.current,
                    "versions": sorted(schema.versions),
                    "column_count": len(schema.version().select),
                    "updated_at": updated_at,
                }
            )
        return rows
