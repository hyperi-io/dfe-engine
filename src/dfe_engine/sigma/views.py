#  Project:      dfe-engine
#  File:         sigma/views.py
#  Purpose:      CRUD-managed Sigma source-view definitions (facade over remap_view)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""CRUD-managed Sigma source-views over the id-keyed gitcrud store.

A Sigma source-view is a ClickHouse VIEW over ONE source's landing table that
exposes standard, Sigma-rule-aligned columns. Where the legacy
``SigmaSourceMapper.generate_sigma_view`` was a one-shot DDL generator driven only
by static field maps (real table column -> Sigma field), a view DEFINITION is a
stored, operator-editable object (gitcrud ``sigma_views`` class, keyed by source
name) that also declares columns DERIVED FROM the source's ``_json`` column.

The view MODEL + DDL generation are now the STANDARD-AGNOSTIC engine in
:mod:`dfe_engine.fieldmap.remap_view` (the same engine ECS + CIM views use); this
module is the thin Sigma facade over it - it keeps the ``sigma_field`` stored
contract + the CRUD store, and delegates rendering. Those JSON-derived columns are
the capability the fixed meta schema lacks: a Sigma field like ``EventID`` that
lives INSIDE the JSON payload is not a real column, so the view extracts it with
the dynamic-subcolumn idiom ``assumeNotNull(_json).`path``` (optionally CAST). The
store reuses the SAME sigma gitcrud registry (``catalog.sigma_registry``) over the
deploy repo, so every view mutation is one attributed git commit - survivability +
audit come free.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from dfe_engine.fieldmap.remap_view import (
    RemapColumn,
    RemapViewDefinition,
    RemapViewError,
    build_remap_view_ddl,
)
from dfe_engine.gitcrud import GitCrud, ResourceNotFoundError
from dfe_engine.gitcrud.commit_policy import CommitContext, build_message

from .catalog import VIEWS_CLASS, sigma_registry

# The Sigma standard name (the view suffix + the remap-view standard).
SIGMA_STANDARD = "sigma"

# Rendering errors surface as SigmaViewError for API/back-compat; it IS the shared
# remap-view error so callers catching either type keep working.
SigmaViewError = RemapViewError


# -- Definition model ----------------------------------------


class SigmaViewColumn(BaseModel):
    """One column exposed by a Sigma source-view.

    A column maps a standard ``sigma_field`` (the alias the view exposes) to EITHER
    a real table column (``source_column``) OR a dotted path inside the source's
    ``_json`` column (``json_path``) - exactly one of the two. ``type`` is an
    optional ClickHouse type the extracted value is CAST to (mainly for a
    JSON-derived column, whose subcolumn is otherwise a ``Dynamic``). This is the
    Sigma-named shape of :class:`~dfe_engine.fieldmap.remap_view.RemapColumn`.
    """

    sigma_field: str = Field(description="Standard Sigma field name (the view alias)")
    source_column: str | None = Field(default=None, description="A real column on the source table")
    json_path: str | None = Field(
        default=None,
        description="A dotted path inside _json (e.g. 'EventID', 'process.command_line')",
    )
    type: str | None = Field(
        default=None,
        description="Optional ClickHouse type to CAST the value to (e.g. 'String', 'UInt32')",
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> SigmaViewColumn:
        """Require exactly one of ``source_column`` / ``json_path`` per column."""
        has_col = bool(self.source_column)
        has_json = bool(self.json_path)
        if has_col == has_json:
            raise ValueError(
                f"column {self.sigma_field!r}: set exactly one of 'source_column' or 'json_path'"
            )
        return self

    @property
    def is_json_derived(self) -> bool:
        """True when this column is extracted from a path inside ``_json``."""
        return bool(self.json_path)

    def to_remap(self) -> RemapColumn:
        """The standard-agnostic form (``sigma_field`` -> ``field``)."""
        return RemapColumn(
            field=self.sigma_field,
            source_column=self.source_column,
            json_path=self.json_path,
            type=self.type,
        )


class SigmaViewDefinition(BaseModel):
    """A CRUD-managed Sigma view definition for one source.

    Keyed (in the store) by ``source_name``; the generated view targets table
    ``source_name`` and is named ``{source_name}_sigma``. ``include_source_columns``
    appends ``SELECT *`` so the base table columns remain visible alongside the
    Sigma-aligned aliases (the legacy view's behaviour).
    """

    source_name: str = Field(description="The _source label the view is for (the store key)")
    description: str = Field(default="", description="Human-readable description")
    columns: list[SigmaViewColumn] = Field(
        default_factory=list, description="Sigma-aligned column mappings"
    )
    include_source_columns: bool = Field(
        default=True, description="Also SELECT * (keep the base table columns in the view)"
    )

    @property
    def json_derived_columns(self) -> list[SigmaViewColumn]:
        """The subset of columns extracted from ``_json``."""
        return [c for c in self.columns if c.is_json_derived]

    def to_remap(self) -> RemapViewDefinition:
        """The standard-agnostic form (fixed ``standard='sigma'``)."""
        return RemapViewDefinition(
            standard=SIGMA_STANDARD,
            source_name=self.source_name,
            description=self.description,
            columns=[c.to_remap() for c in self.columns],
            include_source_columns=self.include_source_columns,
        )


# -- DDL generation (delegates to the shared remap-view engine) ----


def build_sigma_view_ddl(
    definition: SigmaViewDefinition,
    *,
    db: str = "{db}",
    table_name: str | None = None,
) -> str:
    """Render a ``CREATE OR REPLACE VIEW {table}_sigma`` DDL from a stored definition.

    Thin wrapper over :func:`~dfe_engine.fieldmap.remap_view.build_remap_view_ddl`
    with ``standard='sigma'`` - identical output to the former bespoke builder.
    """
    return build_remap_view_ddl(definition.to_remap(), db=db, table_name=table_name)


# -- Store ---------------------------------------------------


def _msg(source_name: str, summary: str, actor: str) -> str:
    """An attributed, conforming commit message (type 'cfg' - sigma maps there)."""
    return build_message(
        CommitContext(ctype="cfg", scope=source_name[:20], summary=summary, actor=actor)
    )


class SigmaViewStore:
    """CRUD over the per-source Sigma view definitions (gitcrud ``sigma_views``).

    One YAML doc per source (key = source name) over the SAME deploy repo as the
    rest of the sigma catalogue, via the sigma-local resource-class registry.
    """

    def __init__(self, crud: GitCrud) -> None:
        # A second GitCrud over the same working clone but with the sigma registry -
        # the same pattern the rule catalogue store uses (see catalog._sigma_crud).
        self._crud = GitCrud(crud.repo, sigma_registry())

    # -- reads --

    def list_sources(self) -> list[str]:
        """Source names that have a stored view definition."""
        return self._crud.list(VIEWS_CLASS)

    def get(self, source_name: str) -> SigmaViewDefinition:
        """Read one view definition; raises ResourceNotFoundError if absent."""
        doc = self._crud.get(VIEWS_CLASS, source_name)
        return SigmaViewDefinition.model_validate(doc)

    def exists(self, source_name: str) -> bool:
        try:
            self._crud.get(VIEWS_CLASS, source_name)
            return True
        except ResourceNotFoundError:
            return False

    def summaries(self) -> list[dict[str, Any]]:
        """Lightweight rows for the list endpoint (counts, not the full column list)."""
        out: list[dict[str, Any]] = []
        for source_name in self.list_sources():
            try:
                definition = self.get(source_name)
            except ResourceNotFoundError:
                continue
            out.append(
                {
                    "source_name": definition.source_name,
                    "description": definition.description,
                    "column_count": len(definition.columns),
                    "json_derived_count": len(definition.json_derived_columns),
                }
            )
        return out

    # -- writes --

    def save(self, definition: SigmaViewDefinition, actor: str) -> SigmaViewDefinition:
        """Create or replace a source's view definition in ONE commit."""
        self._crud.put(
            VIEWS_CLASS,
            definition.source_name,
            definition.model_dump(mode="json"),
            actor,
            message=_msg(definition.source_name, "sigma view definition", actor),
        )
        return definition

    def delete(self, source_name: str, actor: str) -> None:
        """Remove a source's view definition (raises if absent)."""
        self._crud.delete(
            VIEWS_CLASS,
            source_name,
            actor,
            message=_msg(source_name, "delete sigma view", actor),
        )

    # -- generation --

    def generate_ddl(
        self, source_name: str, *, db: str = "{db}", table_name: str | None = None
    ) -> str:
        """Render the view DDL from the stored definition (raises if none stored)."""
        return build_sigma_view_ddl(self.get(source_name), db=db, table_name=table_name)
