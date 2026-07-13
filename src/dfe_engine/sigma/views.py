#  Project:      dfe-engine
#  File:         sigma/views.py
#  Purpose:      CRUD-managed Sigma source-view definitions + DDL generation
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

Those JSON-derived columns are the new capability: the fixed meta schema only knows
``_timestamp_load`` / ``_org_id`` / ``_source`` / ``_raw`` / ``_json``, so a Sigma
field like ``EventID`` that lives INSIDE the JSON payload is not a real column. The
view extracts it with the same dynamic-subcolumn idiom the JSON promotion + sampler
paths use - ``assumeNotNull(_json).`path``` - optionally CAST to a declared type,
and aliases it to the Sigma field name.

The stored definition is the SSoT the DDL is generated FROM (see
``build_sigma_view_ddl`` + ``SigmaViewStore.generate_ddl``); the API's generate
action and ``SigmaSourceMapper`` both prefer a stored definition and fall back to
static field maps when none exists. The store reuses the SAME sigma gitcrud
registry (``catalog.sigma_registry``) over the deploy repo, so every view mutation
is one attributed git commit - survivability + audit come free.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, model_validator

from dfe_engine.gitcrud import GitCrud, ResourceNotFoundError
from dfe_engine.gitcrud.commit_policy import CommitContext, build_message

from .catalog import VIEWS_CLASS, sigma_registry

# Standard JSON column name across all schema profiles. The canonical constant
# lives at services.schema.json_promotion_service.JSON_COLUMN; it is a single
# literal here (as in sampling/clickhouse_reader) to avoid a service-layer import
# from the sigma package - keep the two in step if the column is ever renamed.
JSON_COLUMN = "_json"

# A conservative allow-list for an operator-declared ClickHouse type used verbatim
# in a CAST. Letters/digits/underscore plus the punctuation real CH types need -
# parentheses (parametrised types), commas + spaces (Enum/DateTime args) and single
# quotes (Enum member literals). It deliberately rejects anything that could break
# out of the CAST expression (backticks, parens-mismatch aside, semicolons, ...).
_CH_TYPE_RE = re.compile(r"^[A-Za-z0-9_(), ']+$")


class SigmaViewError(ValueError):
    """Raised when a view definition cannot be rendered to safe DDL."""


# -- Definition model ----------------------------------------


class SigmaViewColumn(BaseModel):
    """One column exposed by a Sigma source-view.

    A column maps a standard ``sigma_field`` (the alias the view exposes) to EITHER
    a real table column (``source_column``) OR a dotted path inside the source's
    ``_json`` column (``json_path``) - exactly one of the two. ``type`` is an
    optional ClickHouse type the extracted value is CAST to (mainly for a
    JSON-derived column, whose subcolumn is otherwise a ``Dynamic``).
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
        """The subset of columns extracted from ``_json`` (Task B)."""
        return [c for c in self.columns if c.is_json_derived]


# -- DDL generation ------------------------------------------


def _safe_ident(name: str, *, what: str) -> str:
    """Validate a bare identifier destined for a backtick-quoted position.

    Rejects a backtick (which would let the value break out of the ``\\`...\\```
    quoting and inject arbitrary DDL) and an empty name. These are governed,
    RBAC'd, git-stored values, but the identifier is interpolated into DDL so it is
    checked as defence in depth - the same discipline json_promotion_service applies
    to its JSON subcolumn accessor.
    """
    if not name:
        raise SigmaViewError(f"empty {what}")
    if "`" in name:
        raise SigmaViewError(f"illegal {what} (backtick): {name!r}")
    return name


def _parens_balanced(s: str) -> bool:
    """True when parentheses are properly nested: the running open-count never
    goes negative and ends at zero. This is what blocks a CAST breakout - a
    premature ``)`` (e.g. ``String) OR (1=1``) closes the CAST early and injects
    the trailer, yet has an equal ()-count, so a bare count check misses it."""
    depth = 0
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _safe_type(ch_type: str) -> str:
    """Validate an operator-declared ClickHouse type for a CAST (allow-list).

    The charset already blocks the obvious injectors (=, ;, backtick, ...); the
    remaining break-out is an unbalanced/premature ``)`` that ends the CAST early,
    so parentheses must be properly nested too.
    """
    if not _CH_TYPE_RE.match(ch_type) or not _parens_balanced(ch_type):
        raise SigmaViewError(f"illegal ClickHouse type: {ch_type!r}")
    return ch_type


def _json_accessor(json_path: str) -> str:
    """Dynamic-subcolumn accessor for a path inside ``_json``.

    Matches the codebase idiom (json_promotion_service._json_subcolumn): the WHOLE
    dotted path is ONE backtick-quoted identifier - that is how the ``JSON`` column
    reports a nested path - and ``assumeNotNull`` unwraps a ``Nullable(JSON)`` column
    (a no-op on a non-nullable one) so subcolumn access type-checks. Rejects a
    backtick to keep it injection-safe.
    """
    if "`" in json_path:
        raise SigmaViewError(f"illegal json_path (backtick): {json_path!r}")
    if not json_path:
        raise SigmaViewError("empty json_path")
    return f"assumeNotNull({JSON_COLUMN}).`{json_path}`"


def _column_select_expr(col: SigmaViewColumn) -> str:
    """Render one column to a ``<expr> AS `<sigma_field>``` SELECT term."""
    alias = _safe_ident(col.sigma_field, what="sigma_field")
    if col.is_json_derived:
        expr = _json_accessor(col.json_path or "")
    else:
        expr = f"`{_safe_ident(col.source_column or '', what='source_column')}`"
    if col.type:
        expr = f"CAST({expr} AS {_safe_type(col.type)})"
    return f"{expr} AS `{alias}`"


def build_sigma_view_ddl(
    definition: SigmaViewDefinition,
    *,
    db: str = "{db}",
    table_name: str | None = None,
) -> str:
    """Render a ``CREATE OR REPLACE VIEW`` DDL from a stored view definition.

    The view is named ``{table}_sigma`` over table ``{table}`` (``table`` defaults
    to the definition's ``source_name``). Each declared column becomes a
    Sigma-aligned alias; a JSON-derived column is extracted from ``_json`` with the
    dynamic-subcolumn idiom (optionally CAST to its declared type). ``{db}`` is a
    placeholder the deployer substitutes, consistent with the schema DDL writer.
    """
    # `db` is normally the "{db}" placeholder the deployer substitutes, but the API
    # generate action passes a real database name (a user-supplied param) - validate
    # it as an identifier so it cannot inject into the DDL (parity with the
    # governance/ch/render quoting seam). The placeholder itself passes through.
    if db != "{db}":
        db = _safe_ident(db, what="db")
    table = _safe_ident(table_name or definition.source_name, what="table_name")
    view_name = f"{table}_sigma"

    select_terms = [_column_select_expr(col) for col in definition.columns]
    if definition.include_source_columns:
        select_terms.append("*")
    if not select_terms:
        select_terms = ["*"]

    body = ",\n    ".join(select_terms)
    return f"CREATE OR REPLACE VIEW {db}.{view_name} AS\nSELECT\n    {body}\nFROM {db}.{table};\n"


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
