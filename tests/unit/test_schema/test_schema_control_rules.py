#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_schema_control_rules.py
#  Purpose:      The guards behind engine-only schema control, as tests not docs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The two static rules of engine-only schema control, enforced rather than stated.

Rule 1 -- the engine is the only controller -- needs a live server and is proven
in ``tests/integration/test_schema/test_manifest_bootstrap.py``.

Rule 2: dfe-schemas is the only schema SSoT. No DDL verb that creates a DECLARED
object appears in a string literal outside ``dfe_engine/schema/``, which is the
package that renders the manifest and applies it. Two families are allowlisted by
file and by verb, each because the object it renders is defined at use time from
an operator's own config rather than declared in the manifest.

Rule 3: nothing schema-shaped is hard-coded. Every database-qualified DFE object
a string literal in ``src/`` names resolves to an object the manifest declares.
That is the failure this rule exists to stop: the engine created ``dfe.default``
for five releases while dfe-schemas declared ``main`` and both e2e suites
asserted it, and the fork pointed a source at a ``dfe_hunts`` database nothing
creates.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from dfe_engine.schema.plan import build_plan

SRC = Path(__file__).resolve().parents[3] / "src" / "dfe_engine"

# The package that renders the manifest and applies it. DDL lives here or nowhere.
APPLIER_PACKAGE = "schema"

# Verbs that bring a DECLARED object into being. Every one of them is the
# manifest's job.
DECLARED_VERBS = (
    "CREATE TABLE",
    "CREATE DATABASE",
    "CREATE MATERIALIZED VIEW",
)

# Verbs for objects a deployment defines at use time: an operator's own tier, a
# per-org or per-group user, a per-source view. Permitted only in the files
# named below, and only those files.
RUNTIME_VERBS = (
    "CREATE OR REPLACE VIEW",
    "CREATE VIEW",
    "CREATE ROLE",
    "CREATE USER",
    "CREATE QUOTA",
    "CREATE SETTINGS PROFILE",
    "ALTER TABLE",
)

RUNTIME_RENDERERS: dict[str, set[str]] = {
    # Tiers, quotas and the per-org and per-group users come from gitops config,
    # so the statements cannot be manifest data.
    "governance/ch/render.py": {
        "CREATE ROLE",
        "CREATE USER",
        "CREATE QUOTA",
        "CREATE SETTINGS PROFILE",
    },
    # One view per source per standard, named from the source's own field map.
    "fieldmap/remap_view.py": {"CREATE OR REPLACE VIEW"},
}


def _statement_re(verb: str) -> re.Pattern[str]:
    """A verb in STATEMENT position: followed by an object, not by prose.

    ``Field(description="CREATE TABLE DDL")`` names the verb and creates nothing,
    so the guard looks for what a statement always carries next -- an
    interpolation, a quoted identifier or a database-qualified name.
    """
    return re.compile(
        verb.replace(" ", r"\s+") + r"(?:\s+IF\s+NOT\s+EXISTS)?\s+(?:[`{\"]|\w+\.)",
        re.IGNORECASE,
    )


# Database-qualified references in a literal: a rendered placeholder or a fixed
# database, then the object. A bare name resolves against whichever database the
# connection is on, so it is not a reference anything can check from the text.
_REFERENCE_RE = re.compile(
    r"(?:`?\{[^{}]*[Dd][Bb][^{}]*\}`?|__DB__|dfe_meta)\.`?(?P<name>[A-Za-z_]\w*)`?"
)

# Object names a literal may qualify that the manifest does not declare, each
# because it is built at runtime rather than shipped.
DYNAMIC_OBJECTS = frozenset(
    {
        # ClickHouse's own introspection, which the manifest does not own.
        "query_log",
        # A source's table and its standard views are named from the source.
        "table",
        "view",
    }
)


def _python_files() -> list[Path]:
    return sorted(path for path in SRC.rglob("*.py") if "cli/auto/vendor" not in path.as_posix())


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Every string Constant that is a docstring, by node id.

    A docstring names DDL to explain it; only an executed literal is a second
    definition.
    """
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.add(id(body[0].value))
    return docstrings


def _joined(node: ast.JoinedStr) -> str:
    """An f-string put back together, with each interpolation as ``{expr}``.

    Reassembling matters: the DDL an engine emits is an f-string, and its
    database placeholder and its table name land in DIFFERENT constant nodes, so
    a guard reading the halves separately can never see the reference.
    """
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            parts.append("{" + ast.unparse(value.value) + "}")
    return "".join(parts)


def _literals(path: Path) -> list[tuple[int, str]]:
    """Every executed string literal in a file, as ``(line, text)``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    skip = _docstring_nodes(tree)
    found: list[tuple[int, str]] = []
    inside_fstring: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            found.append((node.lineno, _joined(node)))
            inside_fstring.update(id(part) for part in ast.walk(node) if part is not node)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
            and id(node) not in inside_fstring
        ):
            found.append((node.lineno, node.value))
    return found


def _relative(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def test_no_declared_object_ddl_outside_the_applier() -> None:
    """Rule 2: only the schema package may create a database, table or MV."""
    offences: list[str] = []
    for path in _python_files():
        relative = _relative(path)
        if relative.startswith(f"{APPLIER_PACKAGE}/"):
            continue
        for line, text in _literals(path):
            for verb in DECLARED_VERBS:
                if _statement_re(verb).search(text):
                    offences.append(f"{relative}:{line}: {verb}")
    assert not offences, (
        "a declared object's DDL lives outside dfe_engine/schema/; it belongs in the "
        "dfe-schemas manifest:\n  " + "\n  ".join(offences)
    )


def test_runtime_object_ddl_only_in_the_files_that_own_it() -> None:
    """Rule 2: an operator-defined object's DDL stays in its one renderer."""
    offences: list[str] = []
    for path in _python_files():
        relative = _relative(path)
        if relative.startswith(f"{APPLIER_PACKAGE}/"):
            continue
        permitted = RUNTIME_RENDERERS.get(relative, set())
        for line, text in _literals(path):
            for verb in RUNTIME_VERBS:
                if verb not in permitted and _statement_re(verb).search(text):
                    offences.append(f"{relative}:{line}: {verb}")
    assert not offences, (
        "DDL for a runtime object appears outside the file that renders it:\n  "
        + "\n  ".join(offences)
    )


@pytest.fixture(scope="module")
def manifest_names() -> frozenset[str]:
    """Every object name the pinned manifest declares, plus the databases."""
    from dfe_engine.settings import load_settings

    plan = build_plan(settings=load_settings())
    names = {rendered.name for rendered in plan.objects}
    names |= {rendered.database for rendered in plan.objects if rendered.database}
    return frozenset(names)


def test_every_qualified_object_reference_is_declared(manifest_names: frozenset[str]) -> None:
    """Rule 3: a literal never names a DFE object the manifest does not declare."""
    offences: list[str] = []
    for path in _python_files():
        relative = _relative(path)
        for line, text in _literals(path):
            for match in _REFERENCE_RE.finditer(text):
                name = match.group("name")
                if name in manifest_names or name in DYNAMIC_OBJECTS:
                    continue
                offences.append(f"{relative}:{line}: {name}")
    assert not offences, (
        "a literal qualifies an object the dfe-schemas manifest does not declare; "
        "add it to the manifest or take the name from config:\n  " + "\n  ".join(offences)
    )


@pytest.mark.xfail(
    reason=(
        "dfe-schemas 0.2.7's renderer drops a column's declared `index` and renders the "
        "use_case template instead, so the manifest gives dfe.main and dfe.detection "
        "TYPE text(tokenizer=ngrams(3)) GRANULARITY 1 where the header asks for "
        "text(tokenizer = 'splitByNonAlpha') GRANULARITY 64. Fixed by teaching dfe_schemas."
        "loader.Column to carry `index` and render.Renderer._index_def to emit it, the "
        "way dfe_engine.schema.schema_ddl.DDLGenerator._index_def already does"
    ),
    strict=False,
)
def test_the_two_renderers_agree_on_the_landing_table_indexes() -> None:
    """A column's declared index must survive the move into the manifest.

    The per-source path and the manifest path render the same common header, so a
    table created by the boot phase and the same table created from a source
    definition must carry the same index or one of them is wrong about what the
    schema asked for.
    """
    from dfe_engine.schema.ddl_writer import DDLFileWriter
    from dfe_engine.settings import load_settings

    def indexes(statement: str) -> list[str]:
        return [line.strip().rstrip(",") for line in statement.splitlines() if "INDEX " in line]

    manifest = build_plan(settings=load_settings()).by_id("data.main").statements[0]
    generated = DDLFileWriter(database="dfe").generate_default_table()
    assert indexes(manifest) == indexes(generated)


def test_the_engine_ships_no_file_the_wheel_already_ships() -> None:
    """Rule 3: no schema data under src/ duplicates a path dfe-schemas ships."""
    from dfe_engine.schema.plan import core_schemas_root

    wheel = core_schemas_root()
    shipped = {path.relative_to(wheel).as_posix() for path in wheel.rglob("*") if path.is_file()}
    duplicates = sorted(
        _relative(path)
        for path in SRC.rglob("*")
        if path.is_file() and path.name in {Path(entry).name for entry in shipped}
    )
    # A name collision on a bare filename is what a second copy looks like; the
    # engine's own modules are .py and the wheel ships .yaml and .sql.
    offending = [entry for entry in duplicates if entry.endswith((".yaml", ".sql"))]
    assert not offending, (
        "the engine ships a file dfe-schemas already ships; delete the copy:\n  "
        + "\n  ".join(offending)
    )
