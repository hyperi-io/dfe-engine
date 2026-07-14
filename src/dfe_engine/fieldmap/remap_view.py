#  Project:      dfe-engine
#  File:         fieldmap/remap_view.py
#  Purpose:      Standard-agnostic remap-view model + DDL (Sigma / ECS / CIM / ...)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Standard-agnostic remap views over a source's landing table.

A remap view is a ClickHouse ``VIEW`` over ONE source's table that re-projects the
source's PHYSICAL columns as the field names of a naming standard (Sigma, ECS,
Splunk CIM, ... - the same pattern for every standard). The view is named
``{table}_{standard}``.

This is the generalisation of what used to live only in ``sigma/views.py``: each
column maps a standard ``field`` (the alias the view exposes) to EITHER a real
table column (``source_column``) OR a dotted path inside the source's ``_json``
column (``json_path``, extracted with the dynamic-subcolumn idiom
``assumeNotNull(_json).`path```), optionally CAST to a declared ClickHouse type.
One engine now serves every standard, so an ECS or CIM view gets the same
json-path + cast capability Sigma had - the importer stays PHYSICAL and the
standard naming is a read-time view (see the elastic importer + the ECS/CIM/Sigma
default maps under ``fieldmap/default_maps/``).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator, model_validator

# Standard JSON column name across all schema profiles. The canonical constant
# lives at services.schema.json_promotion_service.JSON_COLUMN; kept as a single
# literal here (as in sampling/clickhouse_reader) to avoid a service-layer import
# from the fieldmap package - keep the two in step if the column is ever renamed.
JSON_COLUMN = "_json"

_STANDARD_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# A conservative allow-list for an operator-declared ClickHouse type used verbatim
# in a CAST. Letters/digits/underscore plus the punctuation real CH types need -
# parentheses (parametrised types), commas + spaces (Enum/DateTime args) and single
# quotes (Enum member literals). It deliberately rejects anything that could break
# out of the CAST expression (backticks, semicolons, ...).
_CH_TYPE_RE = re.compile(r"^[A-Za-z0-9_(), ']+$")


class RemapViewError(ValueError):
    """Raised when a view definition cannot be rendered to safe DDL."""


# -- Definition model ----------------------------------------


class RemapColumn(BaseModel):
    """One column exposed by a remap view.

    Maps a standard ``field`` (the alias the view exposes) to EITHER a real table
    column (``source_column``) OR a dotted path inside the source's ``_json``
    column (``json_path``) - exactly one of the two. ``type`` is an optional
    ClickHouse type the extracted value is CAST to (mainly for a JSON-derived
    column, whose subcolumn is otherwise a ``Dynamic``).
    """

    field: str = Field(description="Standard field name (the view alias)")
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
    def _exactly_one_source(self) -> RemapColumn:
        """Require exactly one of ``source_column`` / ``json_path`` per column."""
        has_col = bool(self.source_column)
        has_json = bool(self.json_path)
        if has_col == has_json:
            raise ValueError(
                f"column {self.field!r}: set exactly one of 'source_column' or 'json_path'"
            )
        return self

    @property
    def is_json_derived(self) -> bool:
        """True when this column is extracted from a path inside ``_json``."""
        return bool(self.json_path)


class RemapViewDefinition(BaseModel):
    """A remap-view definition for one source under one naming standard.

    The generated view targets table ``source_name`` (override with ``table_name``)
    and is named ``{table}_{standard}``. ``include_source_columns`` appends
    ``SELECT *`` so the base table columns remain visible alongside the
    standard-aligned aliases.
    """

    standard: str = Field(description="Naming standard (sigma, ecs, cim, ...)")
    source_name: str = Field(description="The _source label the view is for")
    description: str = Field(default="", description="Human-readable description")
    columns: list[RemapColumn] = Field(
        default_factory=list, description="Standard-aligned column mappings"
    )
    include_source_columns: bool = Field(
        default=True, description="Also SELECT * (keep the base table columns in the view)"
    )

    @field_validator("standard")
    @classmethod
    def _validate_standard(cls, v: str) -> str:
        v = v.lower()
        if not _STANDARD_RE.match(v):
            raise ValueError(f"standard {v!r} must match [a-z][a-z0-9_]*")
        return v

    @property
    def json_derived_columns(self) -> list[RemapColumn]:
        """The subset of columns extracted from ``_json``."""
        return [c for c in self.columns if c.is_json_derived]


# -- DDL generation ------------------------------------------


def _safe_ident(name: str, *, what: str) -> str:
    """Validate a bare identifier for a backtick-quoted OR an unquoted DDL position.

    Rejects a backtick (which would break out of ``\\`...\\``` quoting), whitespace,
    and the DDL control chars ``; ( ) ' " \\`` - so the value is safe even in the
    UNQUOTED ``{db}.{table}`` positions the view DDL emits, not only the
    backtick-wrapped column aliases. Governed, RBAC'd, git-stored values, checked as
    defence in depth (the discipline json_promotion_service applies to its JSON
    subcolumn accessor).
    """
    if not name:
        raise RemapViewError(f"empty {what}")
    if any(c in name for c in "`;()'\"\\") or any(c.isspace() for c in name):
        raise RemapViewError(f"illegal {what}: {name!r}")
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
        raise RemapViewError(f"illegal ClickHouse type: {ch_type!r}")
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
        raise RemapViewError(f"illegal json_path (backtick): {json_path!r}")
    if not json_path:
        raise RemapViewError("empty json_path")
    return f"assumeNotNull({JSON_COLUMN}).`{json_path}`"


def _column_select_expr(col: RemapColumn) -> str:
    """Render one column to a ``<expr> AS `<field>``` SELECT term."""
    alias = _safe_ident(col.field, what="field")
    if col.is_json_derived:
        expr = _json_accessor(col.json_path or "")
    else:
        expr = f"`{_safe_ident(col.source_column or '', what='source_column')}`"
    if col.type:
        expr = f"CAST({expr} AS {_safe_type(col.type)})"
    return f"{expr} AS `{alias}`"


def build_remap_view_ddl(
    definition: RemapViewDefinition,
    *,
    db: str = "{db}",
    table_name: str | None = None,
) -> str:
    """Render a ``CREATE OR REPLACE VIEW`` DDL from a remap-view definition.

    The view is named ``{table}_{standard}`` over table ``{table}`` (``table``
    defaults to the definition's ``source_name``). Each declared column becomes a
    standard-aligned alias; a JSON-derived column is extracted from ``_json`` with
    the dynamic-subcolumn idiom (optionally CAST to its declared type). ``{db}`` is a
    placeholder the deployer substitutes, consistent with the schema DDL writer.
    """
    # `db` is normally the "{db}" placeholder the deployer substitutes, but a caller
    # may pass a real database name (a user-supplied param) - validate it as an
    # identifier so it cannot inject into the DDL. The placeholder itself passes
    # through.
    if db != "{db}":
        db = _safe_ident(db, what="db")
    table = _safe_ident(table_name or definition.source_name, what="table_name")
    view_name = f"{table}_{definition.standard}"

    select_terms = [_column_select_expr(col) for col in definition.columns]
    if definition.include_source_columns:
        select_terms.append("*")
    if not select_terms:
        select_terms = ["*"]

    body = ",\n    ".join(select_terms)
    return f"CREATE OR REPLACE VIEW {db}.{view_name} AS\nSELECT\n    {body}\nFROM {db}.{table};\n"
