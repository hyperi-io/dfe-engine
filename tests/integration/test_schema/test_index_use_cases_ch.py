#  Project:      dfe-engine
#  File:         tests/integration/test_schema/test_index_use_cases_ch.py
#  Purpose:      Every index use case prunes on a source table built through the engine's schema path
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Each index use case, declared in a meta schema and built the way a source deploy builds it.

dfe-schemas ``tests/test_index_pruning.py`` proves each catalogue index prunes on a
bare one-column table. This is the layer above: the meta schema is composed on the
timeseries common header, so the table carries the header's partitioning, sorting key
and index granularity, and every query carries a time window as real ones do.

``EXPLAIN indexes = 1`` is read in layers. The window drops partitions, the primary
key drops granules inside the partitions left, and the use case's index has to drop
most of what remains. An index whose extra pruning is near zero on its documented
query is not worth its write cost, so that fails here.

The same columns also go on a table with no common header, which a deployment may
choose. There the gate is that the DDL lands and the rows are right. Its pruning is
printed, not asserted.

The rows live in ``tests/support/index_use_cases.py``. A new use case is one row there.
"""

import json
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from dfe_schemas.clickhouse import PlatformProbe, create_database
from dfe_schemas.ddl_catalogue import DDLCatalogue, IndexSpec
from dfe_schemas.loader import DEFAULT_CARDINALITY
from dfe_schemas.render import split_use_case
from scalo.deployment.test_support.pytest_plugin import suite_for
from scalo.deployment.test_support.shared_service import locked

from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2
from dfe_engine.schema.schema_loader import SchemaLoader
from dfe_engine.source.deployment import plan_deploy_statements
from dfe_engine.source.models import Source
from tests.support.clickhouse_cleanup import drop_database_and_policies
from tests.support.index_use_cases import (
    CASES,
    ROWS,
    START,
    STEP_MS,
    WINDOW,
    Case,
    ch_time,
    columns,
    meta_schema,
    row_time,
)

# Loading both tables took 263 s on a remote docker host, against the suite's 300 s default.
_TIMEOUT_S = 900

pytestmark = [pytest.mark.integration, pytest.mark.timeout(_TIMEOUT_S)]

HEADER = ("timeseries", "1.0.3")

# Granules the skip-index stage must receive after partition and PK, so a ratio means something.
MIN_GRANULES = 300

_LOAD_TIME = f"addMilliseconds({ch_time(START)}, n * {STEP_MS})"
_W0 = ch_time(row_time(WINDOW.start))
_W1 = ch_time(row_time(WINDOW.stop))

# Load time drives the partition and the sorting key, event time the console's time
# picker. A real query carries both. Event time trails load time by one second here.
HEADER_WINDOW = (
    f"_timestamp_load >= {_W0} AND _timestamp_load < {_W1} "
    f"AND _timestamp >= {_W0} - INTERVAL 1 MINUTE AND _timestamp < {_W1}"
)
EVENT_WINDOW = f"_timestamp >= {_W0} - INTERVAL 1 MINUTE AND _timestamp < {_W1}"
PLAIN_WINDOW = f"ts >= {_W0} AND ts < {_W1}"

_EVENT_LAG_MS = 1_000
_EVENT_FROM_MS = WINDOW.start * STEP_MS - 60_000
_EVENT_TO_MS = WINDOW.stop * STEP_MS

# The header's own event-time index under the event-time window alone. The partition
# and the sorting key both lead with load time, so neither prunes on this query.
HEADER_CASES: tuple[Case, ...] = (
    Case(
        "header_event_time_window",
        None,
        "1",
        frozenset(
            row
            for row in range(ROWS)
            if _EVENT_FROM_MS <= row * STEP_MS - _EVENT_LAG_MS < _EVENT_TO_MS
        ),
        gate="report",
        index="_timestamp",
        window=EVENT_WINDOW,
    ),
)

# The query condition cache answers a repeated filter from the last run, which hides
# what the indexes themselves would do.
_MEASURE = {"use_query_condition_cache": 0}


@dataclass(frozen=True, slots=True)
class Table:
    """One loaded table, and the time window every query on it carries."""

    database: str
    name: str
    header: bool
    window: str

    @property
    def qualified(self) -> str:
        """The table's backticked, database-qualified name."""
        return f"`{self.database}`.`{self.name}`"


def _source(name: str, meta: Path, *, header: bool) -> Source:
    """A source whose table is the test meta schema, on the timeseries header or on none."""
    profile = {"type": HEADER[0], "version": HEADER[1]} if header else {"type": ""}
    return Source.model_validate(
        {
            "source": name,
            "match": {"field": "tags.collector.type", "value": name},
            "header": profile,
            "schema": {"ttl_days": 0, "engine": "", "meta_schema": str(meta)},
        }
    )


def _deploy(client: Any, database: str, source: Source) -> None:
    """Build the source's table the way a deploy builds it, and run the DDL."""
    builder = SchemaBuilderV2(platform=PlatformProbe(client).platform)
    result = builder.build(source)
    assert not result.validation_errors, result.validation_errors
    statements = plan_deploy_statements(
        builder, source, source.runtime_version_id(), result, db=database, ch_client=client
    ).statements
    assert statements, "the deploy generated no DDL"
    for statement in statements:
        client.command(statement)


def _insert(client: Any, table: Table) -> None:
    values = {"row_id": "n", "ts": _LOAD_TIME} | {c.name: c.value for c in columns()}
    if table.header:
        values |= {
            "_timestamp_load": _LOAD_TIME,
            "_timestamp": f"subtractMilliseconds({_LOAD_TIME}, {_EVENT_LAG_MS})",
            "_timestamp_received": _LOAD_TIME,
            "_org_id": "'org-a'",
            "_source": "'index-probe'",
            "_json": "'{}'",
            "_tags": "'{}'",
        }
    names = ", ".join(f"`{name}`" for name in values)
    client.command(
        f"INSERT INTO {table.qualified} ({names}) SELECT {', '.join(values.values())} "
        f"FROM (SELECT number AS n FROM numbers({ROWS}))"
    )
    # One part per partition, so a part count is a partition count.
    client.command(f"OPTIMIZE TABLE {table.qualified} FINAL")


@contextmanager
def _shared(
    state: Path, load: Callable[[str], None], drop: Callable[[str], None]
) -> Generator[str]:
    """The run's one loaded database on a server, loaded by the first process to ask.

    The suite runs under xdist, which hands this module's tests to several workers.
    Each loading its own copy put four loads on one server and timed them all out.
    Every worker holding the database is counted in *state*, and the last one out
    drops it.
    """
    lock = state.with_suffix(".lock")
    with locked(lock):
        if state.exists():
            held = json.loads(state.read_text(encoding="utf-8"))
            database: str = held["database"]
            users: int = held["users"]
        else:
            database = f"dfe_index_{datetime.now(tz=UTC):%H%M%S%f}"
            try:
                load(database)
            except BaseException:
                with suppress(Exception):
                    drop(database)
                raise
            users = 0
        state.write_text(json.dumps({"database": database, "users": users + 1}), encoding="utf-8")
    try:
        yield database
    finally:
        with locked(lock):
            users = json.loads(state.read_text(encoding="utf-8"))["users"] - 1
            if users:
                state.write_text(
                    json.dumps({"database": database, "users": users}), encoding="utf-8"
                )
            else:
                state.unlink()
                drop(database)


@pytest.fixture(scope="module")
def loaded(
    request: pytest.FixtureRequest,
    module_ch_params: dict,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Any, dict[str, Table]]]:
    """The client, and both tables built through the deploy path with ``ROWS`` rows each."""
    import clickhouse_connect

    # The load outlasts the driver's 300 s read default. No session, so a query left
    # running after a client error cannot lock out the cleanup.
    client = clickhouse_connect.get_client(
        **module_ch_params, send_receive_timeout=_TIMEOUT_S, autogenerate_session_id=False
    )
    meta = tmp_path_factory.mktemp("index-use-cases") / "index_use_cases.yaml"
    meta.write_text(yaml.safe_dump(meta_schema(), sort_keys=False), encoding="utf-8")
    sources = {
        "header": _source("index-probe", meta, header=True),
        "plain": _source("index-probe-plain", meta, header=False),
    }
    windows = {"header": HEADER_WINDOW, "plain": PLAIN_WINDOW}

    def tables(database: str) -> dict[str, Table]:
        return {
            key: Table(database, source.table_name, key == "header", windows[key])
            for key, source in sources.items()
        }

    def load(database: str) -> None:
        client.command(create_database(database, PlatformProbe(client).platform))
        for key, table in tables(database).items():
            _deploy(client, database, sources[key])
            _insert(client, table)

    run_dir = suite_for(request.config).run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    server = "".join(c if c.isalnum() else "-" for c in str(module_ch_params["host"]))
    state = run_dir / f"index-use-cases-{server}-{module_ch_params['port']}.json"
    try:
        with _shared(
            state, load, lambda database: drop_database_and_policies(client, database)
        ) as database:
            yield client, tables(database)
    finally:
        client.close()


# -- reading the plan -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Layer:
    """What one pruning step received and kept."""

    initial: int
    selected: int

    def __str__(self) -> str:
        return f"{self.selected}/{self.initial}"


@dataclass(frozen=True, slots=True)
class Pruning:
    """The plan's pruning, layer by layer.

    Attributes:
        parts: Parts the partition key kept, or None with no partition key.
        primary_key: Granules the primary key kept, or None with no sorting key.
        index: The granules the skip-index stage received, and what the case's own
            index left of them. None when the plan never used that index.
        others: Every other skip index the plan applied, by name.
    """

    parts: Layer | None
    primary_key: Layer | None
    index: Layer | None
    others: dict[str, Layer] = field(default_factory=dict)

    def __str__(self) -> str:
        others = " ".join(f"{name}={layer}" for name, layer in self.others.items())
        return (
            f"parts {self.parts} | pk granules {self.primary_key} | index granules "
            f"{self.index}{' | ' + others if others else ''}"
        )


def _plan_indexes(client: Any, sql: str) -> list[dict[str, Any]]:
    rows = client.query(f"EXPLAIN indexes = 1, json = 1 {sql}", settings=_MEASURE).result_rows
    plan = json.loads("\n".join(str(row[0]) for row in rows))[0]["Plan"]
    found: list[dict[str, Any]] = []
    stack = [plan]
    while stack:
        node = stack.pop()
        found += node.get("Indexes", [])
        stack += node.get("Plans", [])
    return found


def _pruning(client: Any, sql: str, index_names: set[str]) -> Pruning:
    entries = _plan_indexes(client, sql)
    part_steps = [e for e in entries if e.get("Type") in ("MinMax", "Partition")]
    parts = (
        Layer(part_steps[0]["Initial Parts"], part_steps[-1]["Selected Parts"])
        if part_steps
        else None
    )
    pk = [e for e in entries if e.get("Type") == "PrimaryKey"]
    primary_key = Layer(pk[0]["Initial Granules"], pk[0]["Selected Granules"]) if pk else None
    skips = [e for e in entries if e.get("Type") == "Skip"]
    ours = [e for e in skips if e.get("Name") in index_names]
    index = None
    if ours:
        received = primary_key.selected if primary_key else skips[0]["Initial Granules"]
        index = Layer(received, ours[-1]["Selected Granules"])
    others = {
        str(e["Name"]): Layer(e["Initial Granules"], e["Selected Granules"])
        for e in skips
        if e.get("Name") not in index_names
    }
    return Pruning(parts, primary_key, index, others)


# -- the catalogue ----------------------------------------------------------

# ClickHouse rewrites a text index's GRANULARITY to this whatever is asked for.
_TEXT_INDEX_GRANULARITY = 100_000_000


@pytest.fixture(scope="module")
def catalogue() -> DDLCatalogue:
    """The DDL catalogue the installed dfe-schemas ships."""
    return DDLCatalogue.load()


def _declared(case: Case) -> tuple[str, int | None, str]:
    """The case's use case, its argument and its cardinality as the catalogue reads them."""
    if case.column is not None:
        declared = case.column.declare
        use_case, dims = split_use_case(declared["use_case"])
        cardinality = declared.get("cardinality", DEFAULT_CARDINALITY)
    else:
        profile = SchemaLoader.load_profile(HEADER[0], profile_version=HEADER[1])
        column = next(c for c in profile if c.name == case.index_column)
        use_case, dims = split_use_case(column.use_case)
        cardinality = column.declared_cardinality
    assert use_case is not None, f"{case.id}: {case.index_column} declares no use case"
    return use_case, dims, cardinality


def _squash(text: str) -> str:
    return "".join(text.split()).replace("`", "")


def _index_name(column: str, spec: IndexSpec) -> str:
    return f"idx_{column}" if spec.suffix is None else f"idx_{column}_{spec.suffix}"


def _nullable(client: Any, table: Table, column: str) -> bool:
    """Whether the server's type for *column* is Nullable, which picks a spec's ``when_nullable``."""
    kind = client.query(
        "SELECT type FROM system.columns "
        "WHERE database = {db:String} AND table = {tbl:String} AND name = {col:String}",
        parameters={"db": table.database, "tbl": table.name, "col": column},
    ).result_rows[0][0]
    return "Nullable(" in str(kind)


def _expected_indexes(
    catalogue: DDLCatalogue, case: Case, *, nullable: bool
) -> dict[str, tuple[str, str, int]]:
    """Index name -> (expression, type, granularity) as the server should report the catalogue's answer."""
    use_case, dims, cardinality = _declared(case)
    expected: dict[str, tuple[str, str, int]] = {}
    for spec in catalogue.specs(use_case, cardinality=cardinality):
        chosen = spec.when_nullable if (nullable and spec.when_nullable) else spec
        index_type = chosen.type.replace("{dims}", str(dims)) if chosen.needs_dims else chosen.type
        granularity = (
            _TEXT_INDEX_GRANULARITY if index_type.startswith("text(") else chosen.granularity
        )
        expected[_index_name(case.index_column, spec)] = (
            _squash(spec.expression.replace("{col}", case.index_column)),
            _squash(index_type),
            granularity,
        )
    return expected


def _live_indexes(client: Any, table: Table, names: set[str]) -> dict[str, tuple[str, str, int]]:
    rows = client.query(
        "SELECT name, expr, type_full, granularity FROM system.data_skipping_indices "
        "WHERE database = {db:String} AND table = {tbl:String}",
        parameters={"db": table.database, "tbl": table.name},
    ).result_rows
    return {
        str(name): (_squash(str(expr)), _squash(str(kind)), int(granularity))
        for name, expr, kind, granularity in rows
        if name in names
    }


# -- the tests --------------------------------------------------------------


def _query(table: Table, case: Case) -> str:
    window = case.window if (table.header and case.window) else table.window
    return f"SELECT row_id FROM {table.qualified} WHERE ({window}) AND ({case.where}) {case.tail}"


def _ids(client: Any, sql: str, **settings: int) -> frozenset[int]:
    rows = client.query(sql, settings=_MEASURE | settings).result_rows
    return frozenset(int(row[0]) for row in rows)


def _check(
    loaded: tuple[Any, dict[str, Table]], catalogue: DDLCatalogue, case: Case, key: str
) -> Pruning:
    """Gate the DDL and the rows, and return the plan's pruning for the caller to judge."""
    client, tables = loaded
    table = tables[key]
    nullable = _nullable(client, table, case.index_column)
    expected = _expected_indexes(catalogue, case, nullable=nullable)

    live = _live_indexes(client, table, set(expected))
    assert live == expected, f"{case.id}: the server holds {live}, the catalogue says {expected}"

    sql = _query(table, case)
    # The data is what the row says it is, read with no skip index at all.
    assert _ids(client, sql, use_skip_indexes=0) == case.expected, f"{case.id}: bad fixture"
    got = _ids(client, sql)
    missing, extra = sorted(case.expected - got), sorted(got - case.expected)
    assert not missing, f"{case.id}: {len(missing)} rows missing, e.g. {missing[:5]}"
    assert not extra, f"{case.id}: {len(extra)} rows that do not match, e.g. {extra[:5]}"

    pruning = _pruning(client, sql, set(expected))
    print(f"\n[{key}] {case.id}: {pruning}")
    return pruning


@pytest.mark.parametrize("case", [pytest.param(c, id=c.id) for c in (*CASES, *HEADER_CASES)])
def test_an_index_prunes_beyond_the_common_header(loaded, catalogue, case: Case) -> None:
    """On the header's partitions and sorting key, the use case's index still drops most granules.

    Rows are exact: a bloom filter's false positive is allowed in the granules read,
    never in the result.
    """
    pruning = _check(loaded, catalogue, case, "header")

    if case.window is None:
        parts, pk = pruning.parts, pruning.primary_key
        assert parts is not None, f"{case.id}: no partition key in the plan: {pruning}"
        assert parts.selected < parts.initial, f"{case.id}: the window kept every partition"
        assert pk is not None, f"{case.id}: no primary key in the plan: {pruning}"
        assert pk.selected < pk.initial, f"{case.id}: the primary key pruned nothing: {pruning}"
    if case.gate == "report":
        return
    index = pruning.index
    if case.gate == "no_prune":
        assert index is None or index.selected > index.initial * 0.9, (
            f"{case.id}: the documented anti-pattern pruned {index}, so the docs are wrong"
        )
        return
    assert index is not None, f"{case.id}: the plan never used its index: {pruning}"
    if case.gate == "prunes":
        assert index.initial >= MIN_GRANULES, f"{case.id}: only {index.initial} granules reached it"
        assert index.selected < index.initial * 0.1, f"{case.id}: the index kept {index}"


@pytest.mark.parametrize("case", [pytest.param(c, id=c.id) for c in CASES])
def test_an_index_lands_and_answers_without_a_header(loaded, catalogue, case: Case) -> None:
    """With no common header the DDL lands and the rows are right. Pruning is printed only."""
    _check(loaded, catalogue, case, "plain")
