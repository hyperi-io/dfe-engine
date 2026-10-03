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

import builtins
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


def shipped_derived_directory(settings: Any) -> Path | None:
    """Where the release's own derived-schema documents sit, gitops aside.

    Always ``<schemas_dir>/derived`` -- the second root ``resolve_derived_reference``
    falls back to for the build. ``list`` and ``get`` read it too, so a name the
    deploy repo does not carry is never missing from the API.
    """
    schemas_dir = getattr(settings.schemas, "schemas_dir", None)
    return Path(schemas_dir) / DERIVED_PREFIX if schemas_dir else None


class DerivedSchemaRegistry:
    """CRUD over derived-schema documents."""

    def __init__(
        self,
        derived_directory: str | Path | None = None,
        *,
        crud: GitCrud | None = None,
        shipped_directory: str | Path | None = None,
    ) -> None:
        """Bind the store to a backend.

        Args:
            derived_directory: ``<schemas_dir>/derived`` for the directory
                backend. Ignored when ``crud`` is given.
            crud: Governed Ops engine over the deploy repo. When provided,
                ``config/schemas/derived/`` is the SSoT and every mutation is
                one git commit.
            shipped_directory: The release's own ``<schemas_dir>/derived``,
                read as a second root for ``list`` and ``get``. ``None`` when
                the deployment configures no schemas tree.
        """
        self._crud = crud
        self._shipped_directory = (
            Path(shipped_directory).expanduser().resolve(strict=False)
            if shipped_directory is not None
            else None
        )
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
        shipped = shipped_derived_directory(settings)
        if crud is not None:
            return cls(crud=crud, shipped_directory=shipped)
        return cls(derived_directory(settings), shipped_directory=shipped)

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

    def _path_under(self, root: Path, key: str) -> Path:
        # Append rather than with_suffix: a name carrying a dot keeps it.
        segments = key.split("/")
        candidate = root.joinpath(*segments[:-1]) / f"{segments[-1]}.yaml"
        base = root.resolve(strict=False)
        if not candidate.resolve(strict=False).is_relative_to(base):
            raise DerivedSchemaValidationError("Derived schema path escapes its directory")
        return candidate

    def _yaml_path(self, key: str) -> Path:
        return self._path_under(self._directory, key)

    def _effective_shipped_root(self) -> Path | None:
        """The shipped root, or None when absent or identical to this store's own.

        Gitops off resolves both to the same directory, so a second pass would
        only relist every row under the wrong origin.
        """
        if self._shipped_directory is None:
            return None
        if self._shipped_directory == self._directory.resolve(strict=False):
            return None
        return self._shipped_directory

    def _shipped_names(self) -> builtins.list[str]:
        root = self._effective_shipped_root()
        if root is None or not root.is_dir():
            return []
        return sorted(
            p.relative_to(root).as_posix()[: -len(".yaml")]
            for p in root.glob("**/*.yaml")
            if p.is_file()
        )

    def _get_shipped_raw(self, key: str) -> dict[str, Any] | None:
        root = self._effective_shipped_root()
        if root is None:
            return None
        path = self._path_under(root, key)
        if not path.is_file():
            return None
        return yaml_load(path) or {}

    # -----------------------------------------------------------------
    # Backend primitives
    # -----------------------------------------------------------------

    def _names(self) -> builtins.list[str]:
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
        """Read one derived schema, the deploy repo first then the shipped tree.

        The same precedence :func:`resolve_derived_reference` gives the build,
        so a source can never bind a derived schema this read reports missing.

        Raises:
            DerivedSchemaNotFoundError: Nothing stored at that path in either root.
            DerivedSchemaValidationError: The stored document no longer parses.
        """
        key = canonical_derived_path(path)
        raw = self._get_raw(key)
        shipped = raw is None
        if shipped:
            raw = self._get_shipped_raw(key)
        if raw is None:
            raise DerivedSchemaNotFoundError(f"Derived schema not found: {key!r}")
        try:
            schema = DerivedSchema.model_validate(raw)
        except Exception as exc:
            raise DerivedSchemaValidationError(f"Invalid derived schema {key!r}: {exc}") from exc
        schema.path = derived_reference(key)
        if shipped:
            schema.origin = "shipped"
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

    def list(self) -> builtins.list[dict[str, Any]]:
        """Every stored derived schema, the deploy repo first then the shipped tree.

        A name in both roots is listed once, from the deploy repo -- the same
        precedence :func:`resolve_derived_reference` gives the build. A document
        that no longer parses is left out rather than failing the listing, the
        way the meta-schema and source listings do.
        """
        rows: builtins.list[dict[str, Any]] = []
        seen: set[str] = set()
        for key in self._names():
            row = self._row(key, raw=self._get_raw(key), root=self._directory, origin="deploy")
            if row is not None:
                rows.append(row)
                seen.add(key)
        shipped_root = self._effective_shipped_root()
        if shipped_root is not None:
            for key in self._shipped_names():
                if key in seen:
                    continue
                row = self._row(
                    key, raw=self._get_shipped_raw(key), root=shipped_root, origin="shipped"
                )
                if row is not None:
                    rows.append(row)
        return rows

    def _row(
        self, key: str, *, raw: dict[str, Any] | None, root: Path, origin: str
    ) -> dict[str, Any] | None:
        """One list row from one root, or None when the document no longer parses."""
        if raw is None:
            return None
        try:
            schema = DerivedSchema.model_validate(raw)
        except Exception as exc:
            logger.warning(f"Failed to parse derived schema {key!r}, excluded from list: {exc}")
            return None
        try:
            updated_at = datetime.fromtimestamp(
                self._path_under(root, key).stat().st_mtime, tz=UTC
            ).isoformat()
        except OSError:
            updated_at = ""
        return {
            "path": derived_reference(key),
            "base": schema.base,
            "base_version": schema.base_version,
            "current": schema.current,
            "versions": sorted(schema.versions),
            "column_count": len(schema.version().select),
            "updated_at": updated_at,
            "origin": origin,
        }
