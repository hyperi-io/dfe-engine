#  Project:      dfe-engine
#  File:         tests/e2e/test_data_evolution.py
#  Purpose:      The data-evolution path proved step by step against a live deployment
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One test per step of docs/data-evolution-acceptance.md.

The path is a SEQUENCE - a record lands with no schema, a field is promoted out
of ``_json``, the source gets its own routing and table, an index moves on the
live table - so it is a module of ordered tests rather than a flow fixture. The
flow suite's ``expect.yaml`` carries six keys (source, table, topic, load_topic,
marker, destination) and none of them can say "this typed column carries this
value", "this nested ``_json`` path answers", "this write is refused" or "the
index set changed and the parts did not".

A step the product cannot do yet is ``xfail(strict=True)`` naming its issue, so
the suite goes red the day the fix lands and someone removes the marker. An
assertion is never softened to make a run green - catching exactly that is what
the path is for.

Every skip is one a fixture declared, through ``require`` or a missing corpus.
Nothing here skips from a test body.

Run it the way README-live.md documents, in ONE worker: the steps share a source
and a table, so ``-n`` splits the sequence across processes that then fight over
it.

    uv run pytest tests/e2e/test_data_evolution.py -m live -o addopts=""
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from tests.e2e import filebeat_corpus as corpus
from tests.e2e.conftest import (
    E2EConfig,
    cluster_name,
    count_rows,
    drop_table,
    must,
    poll_until,
    post_events,
    require,
)
from tests.e2e.engine_api import EngineAPI

# What a record gets to make the whole trip. A new source's FIRST record waits
# on dfe-loader's topic refresh, which runs on a 60 s interval, so anything
# shorter than that manufactures a failure the product did not commit.
LANDING_DEADLINE = 300.0

# dfe-loader caches a table's column directives for 300 s, so no claim about a
# schema change reaching the loader means anything inside that window.
DIRECTIVE_CACHE_TTL = 300.0

# A promoted column is judged only on a record sent after the directive cache
# could have turned over, which is the two waits end to end.
PROMOTION_DEADLINE = DIRECTIVE_CACHE_TTL + LANDING_DEADLINE

# pyproject's 300 s hang guard is shorter than one of these waits and its thread
# method kills the process rather than failing the case, so this module carries
# its own, above the deadlines that are the real guard.
SUITE_TIMEOUT = PROMOTION_DEADLINE + LANDING_DEADLINE * 2 + 300.0

pytestmark = [pytest.mark.live, pytest.mark.timeout(SUITE_TIMEOUT)]

# The catch-all a record with no source of its own lands in. A deployment may
# rename it, which the flow suite reads off the receiver and the loader; the
# stage 0 steps assert the default so they need no engine and skip on the
# receiver and ClickHouse vars alone.
LANDING_TABLE = "main"

# The source the stage 1 and stage 2 steps evolve. A source name is a DNS-1123
# label because a source-bound app is deployed one instance per source, so no
# underscore.
SOURCE = "dataevolution"
MATCH_FIELD = "tags.feed"
MATCH_VALUE = SOURCE

# meta/syslog names typed columns a plain JSON record fills, which is what step
# 2.3 reads back. No version is pinned: an absent pin resolves to the schema's
# own current, so the suite carries no version number to go stale.
META_SCHEMA = "meta/syslog"

# The pre-supplied schema step 1.1 reads, and the table step 2.7's transform
# fills.
SHIPPED_SCHEMA = "meta/beats/filebeat"

# Declared by SHIPPED_SCHEMA and set by the bundled filebeat VRL for all three
# corpus modules, so it is the column that says a syslog line was parsed.
PARSED_COLUMN = "source_ip"

# Step 2.7's own source. Named without the catalogue entry's underscore for the
# DNS-1123 reason above; dfe-transform-elastic carries the entry name separately
# in `transform.variant`, so the two never have to agree.
TRANSFORM_SOURCE = "ciscoios"
TRANSFORM_MODULE = "cisco_ios"

# Step 1.2. A custom schema of its own, because a `core` one exports as a
# REFERENCE by design and a reference round-trips nothing.
ROUNDTRIP_SOURCE = "meta/acceptance_roundtrip_src"
ROUNDTRIP_TARGET = "meta/acceptance_roundtrip_dst"
ROUNDTRIP_VERSION = "1.0.0"
_COMPARED_FIELDS = ("name", "type", "use_case", "expr")

# Steps 2.4 and 2.5. The derived schema selects five of SHIPPED_SCHEMA's twelve
# columns; DERIVED_DROPPED is the other seven, and its absence from the deployed
# table is what says the selection reached the data plane.
NARROW_SOURCE = "acceptnarrow"
DERIVED_PATH = "derived/accept/filebeat_narrow"
DERIVED_VERSION = "1.0.0"
SHIPPED_SCHEMA_VERSION = "1.0.0"

# name -> the `index` we send. None omits the key, which keeps the base column's
# own use case and reads differently from the string "none", which keeps the
# column and drops its index.
DERIVED_SELECT: dict[str, str | None] = {
    "timestamp": None,
    "host_name": "exact_match",
    "event_dataset": None,
    "source_ip": "none",
    "message": "substring_search",
}
DERIVED_DROPPED = (
    "agent_type",
    "agent_version",
    "event_module",
    "log_file_path",
    "user_name",
    "process_name",
    "process_pid",
)

# Step 2.8's source. Compose runs ONE instance per transform app and binds it to
# a source at deploy, so this names the source that already has one rather than
# creating a second the deployment cannot serve.
SWAP_SOURCE = "filebeat"

# Stage 3. Its own source, because it turns population off on the table it owns
# and the stage 2 steps read `_json` on theirs.
CAPTURE_SOURCE = "acceptcapture"
CAPTURE_DERIVED = "derived/accept/capture_off"
CAPTURE_VERSION = "1.0.0"
CAPTURE_SELECT = ("timestamp", "host_name", "event_dataset", "message")

# What an UNPOPULATED `_json` reads as. The column is non-Nullable JSON, so the
# loader writing nothing leaves the empty object rather than NULL: `toString()`
# renders `{}` and `length()` is 2. A falsiness check on that length can never
# hold, so it would xfail forever and never go red when #513 lands.
EMPTY_JSON_LENGTH = 2

HEADER = {"type": "common-header/timeseries", "version": "1.0.1"}


# --- Helpers ----------------------------------------------------------------


def _scalar(ch_client, sql: str, parameters: dict[str, Any] | None = None) -> Any:
    """The first cell of a one-row query, or None when it returned nothing."""
    rows = ch_client.query(sql, parameters=parameters or None).result_rows
    return rows[0][0] if rows else None


def _row(ch_client, sql: str, parameters: dict[str, Any] | None = None) -> tuple | None:
    """The first row of a query, or None when it returned nothing."""
    rows = ch_client.query(sql, parameters=parameters or None).result_rows
    return tuple(rows[0]) if rows else None


def _deploy(engine: EngineAPI, name: str) -> dict[str, Any]:
    """Deploy the source's CURRENT version, named explicitly.

    A bare deploy takes ``deployed_version``, so after a promote has created a
    new version a bare call re-deploys the old one, answers 200 and changes
    nothing.
    """
    current = str(engine.json("GET", f"/sources/{name}")["current"])
    return engine.json("POST", f"/sources/{name}/deploy?version={current}")


def _delete(engine: EngineAPI, name: str) -> None:
    """Remove a source, tolerating one that is not there."""
    response = engine.call("DELETE", f"/sources/{name}")
    assert response.status_code in (200, 204, 404), (
        f"could not remove {name}: {response.status_code} {response.text}"
    )


def _alter(ch_client, statement: str) -> None:
    """Run an ALTER on every replica of the cluster the server declares.

    Without ON CLUSTER the change lands on the one replica the connection
    reached, so the other replicas answer a query the index is meant to serve
    without it.
    """
    cluster = cluster_name(ch_client)
    on_cluster = f" ON CLUSTER {cluster}" if cluster else ""
    ch_client.command(statement.replace("{on_cluster}", on_cluster))


def _transform_engine(cfg: E2EConfig) -> str:
    """The engine name for the transform app the deployment runs."""
    return (cfg.transform or "dfe-transform-vrl").removeprefix("dfe-transform-")


def _nested(field: str, value: Any) -> dict:
    """A record fragment that satisfies a dotted match field.

    The receiver splits the field on ``.`` and walks the raw payload, so
    ``tags.feed`` has to arrive as a nested object rather than a flat key.
    """
    body: Any = value
    for part in reversed(field.split(".")):
        body = {part: body}
    return body


def _write_capture_version(engine: EngineAPI, *, capture_json: bool) -> None:
    """Write stage 3's derived version with both capture switches set together.

    Both or neither: json alone has no dfe-loader mode and the version model
    refuses the pair before a document can carry it.
    """
    body = {
        "base": SHIPPED_SCHEMA,
        "base_version": SHIPPED_SCHEMA_VERSION,
        "current": CAPTURE_VERSION,
        "versions": {
            CAPTURE_VERSION: {
                "date": "2026-09-23",
                "summary": f"Acceptance stage 3 -- capture_json={capture_json}",
                "capture_json": capture_json,
                "capture_raw": capture_json,
                "select": [{"name": name} for name in CAPTURE_SELECT],
            }
        },
    }
    written = engine.call("PUT", f"/schemas/definitions/derived/{CAPTURE_DERIVED}", body)
    if written.status_code >= 300:
        written = engine.call("POST", f"/schemas/definitions/derived/{CAPTURE_DERIVED}", body)
    assert written.status_code in (200, 201), (
        f"the deployment refused the capture schema: {written.status_code} {written.text}"
    )


def _column_signatures(columns: Any) -> list[tuple]:
    """One comparable tuple per column, over the fields an import must preserve."""
    items = columns["items"] if isinstance(columns, dict) else columns
    return [tuple(column.get(field) for field in _COMPARED_FIELDS) for column in items]


@dataclass(frozen=True)
class Evolved:
    """The source the stage 1 and stage 2 steps evolve, and how it was made."""

    name: str
    table: str
    """Database-qualified, as a query names it."""

    stored: dict[str, Any]
    """GET /sources/{name} as it read back after the create."""

    deployed: dict[str, Any]
    """The deploy result, so step 2.2 asserts what the deploy itself reported."""

    def version(self) -> dict[str, Any]:
        """The current version snapshot out of the stored definition."""
        return dict(self.stored["versions"][str(self.stored["current"])])


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def engine(e2e: E2EConfig) -> EngineAPI:
    """The engine API, or the skip that says which var is missing."""
    require(e2e, "engine_url", "engine_password")
    api = EngineAPI(
        base=must(e2e.engine_url).rstrip("/"),
        user=e2e.engine_user,
        password=must(e2e.engine_password),
        verify=e2e.verify,
    )
    api.login()
    return api


@pytest.fixture(scope="module")
def evolved(engine: EngineAPI, e2e: E2EConfig, ch_client):
    """A source of its own with a meta schema, created and deployed once.

    Created here rather than per test because the steps are a sequence: 1.3
    promotes a field onto it, 2.3 reads its typed columns, and 2.6 moves an
    index on the table those records are in.
    """
    require(e2e, "receiver_url", "ch_host")
    # The table goes too: a CREATE IF NOT EXISTS over a crashed run's leftover
    # inherits its promoted columns and its indexes, which 1.3 and 2.6 then read.
    _delete(engine, SOURCE)
    drop_table(ch_client, e2e.ch_db, SOURCE)
    created = engine.call(
        "POST",
        "/sources",
        {
            "source": SOURCE,
            "display_name": "Data evolution acceptance",
            "description": "The acceptance path: routing, a typed table, promote and an index.",
            "match": {"field": MATCH_FIELD, "operator": "equals", "value": MATCH_VALUE},
            "header": HEADER,
            "schema": {"meta_schema": META_SCHEMA},
        },
    )
    assert created.status_code == 201, (
        f"the deployment refused the source: {created.status_code} {created.text}"
    )
    state = Evolved(
        name=SOURCE,
        table=f"{e2e.ch_db}.{SOURCE}",
        stored=engine.json("GET", f"/sources/{SOURCE}"),
        deployed=_deploy(engine, SOURCE),
    )
    yield state
    _delete(engine, SOURCE)
    drop_table(ch_client, e2e.ch_db, SOURCE)


@pytest.fixture(scope="module")
def swapped_table(engine: EngineAPI, e2e: E2EConfig, ch_client) -> tuple:
    """Step 2.8's source: one that ALREADY has a transform instance behind it.

    Not created here. Compose declares one service per transform app and binds it
    to a source at deploy, so a second transform-bearing source is refused and a
    freshly created one has nothing running in front of it. The step is about a
    different ENGINE on the same corpus, which this deployment already provides.
    """
    require(e2e, "receiver_url", "ch_host")
    found = engine.call("GET", f"/sources/{SWAP_SOURCE}")
    if found.status_code != 200:
        pytest.skip(f"{SWAP_SOURCE} is not deployed here, so no second engine to swap to")
    table = f"{e2e.ch_db}.{SWAP_SOURCE}"
    exists = _scalar(
        ch_client,
        "SELECT count() FROM system.tables WHERE database = %(d)s AND name = %(t)s",
        {"d": e2e.ch_db, "t": SWAP_SOURCE},
    )
    if not exists:
        pytest.skip(f"{table} does not exist, so {SWAP_SOURCE} has never been deployed")

    # Read the rule rather than assume it: this source belongs to the deployment,
    # so its match field and value are not this suite's to choose.
    stored = found.json()
    match = stored["versions"][str(stored["current"])].get("match")
    if not match:
        pytest.skip(f"{SWAP_SOURCE} declares no match rule, so nothing can be routed to it")
    return table, match


@pytest.fixture(scope="module")
def capture_off(engine: EngineAPI, e2e: E2EConfig, ch_client):
    """Stage 3's source, deployed with `_json` and `_raw` population switched OFF."""
    require(e2e, "receiver_url", "ch_host")
    _delete(engine, CAPTURE_SOURCE)
    drop_table(ch_client, e2e.ch_db, CAPTURE_SOURCE)
    _write_capture_version(engine, capture_json=False)
    created = engine.call(
        "POST",
        "/sources",
        {
            "source": CAPTURE_SOURCE,
            "display_name": "Capture acceptance",
            "description": "A source whose table stops carrying _json and _raw.",
            "match": {
                "field": MATCH_FIELD,
                "operator": "equals",
                "value": CAPTURE_SOURCE,
            },
            "header": HEADER,
            "schema": {
                "meta_schema": SHIPPED_SCHEMA,
                "meta_schema_version": SHIPPED_SCHEMA_VERSION,
                "derived_schema": CAPTURE_DERIVED,
                "derived_schema_version": CAPTURE_VERSION,
            },
        },
    )
    assert created.status_code == 201, (
        f"the deployment refused the capture source: {created.status_code} {created.text}"
    )
    _deploy(engine, CAPTURE_SOURCE)
    yield CAPTURE_SOURCE
    _delete(engine, CAPTURE_SOURCE)
    drop_table(ch_client, e2e.ch_db, CAPTURE_SOURCE)
    engine.call("DELETE", f"/schemas/definitions/derived/{CAPTURE_DERIVED}")


@pytest.fixture(scope="module")
def exported_schema(engine: EngineAPI) -> dict:
    """A custom meta schema, exported and re-pathed ready to import.

    Built here rather than borrowed from the deployment: a `core` schema exports
    as a reference by design, and a custom one belonging to something else
    carries whatever that thing last did to it.
    """
    bundle = {
        "kind": "meta_schema",
        "format": 1,
        "path": ROUNDTRIP_SOURCE,
        "resource_type": "custom",
        "current": ROUNDTRIP_VERSION,
        "versions": {
            ROUNDTRIP_VERSION: {
                "date": "2026-09-23",
                "type": "model",
                "summary": "Acceptance 1.2 -- the export/import round trip",
                "columns": [
                    {
                        "name": "timestamp",
                        "type": "datetime",
                        "use_case": "range",
                        "expr": "@source: @timestamp",
                        "comment": "Event timestamp",
                    },
                    {
                        "name": "host_name",
                        "type": "string",
                        "use_case": "dimension",
                        "expr": "@source: host.name",
                        "comment": "Host the event came from",
                    },
                    {
                        "name": "message",
                        "type": "text",
                        "use_case": "word_search",
                        "comment": "The event text",
                    },
                ],
            }
        },
    }
    for path in (ROUNDTRIP_SOURCE, ROUNDTRIP_TARGET):
        engine.call("DELETE", f"/schemas/definitions/{path}")
    seeded = engine.call("POST", "/schemas/import", bundle)
    assert seeded.status_code in (200, 201), (
        f"the deployment refused the seed schema: {seeded.status_code} {seeded.text}"
    )

    exported = engine.json(
        "GET",
        f"/schemas/definitions/{ROUNDTRIP_SOURCE}/export?version={ROUNDTRIP_VERSION}",
    )
    exported["path"] = ROUNDTRIP_TARGET
    yield exported
    for path in (ROUNDTRIP_SOURCE, ROUNDTRIP_TARGET):
        engine.call("DELETE", f"/schemas/definitions/{path}")


@pytest.fixture(scope="module")
def narrowed(engine: EngineAPI, e2e: E2EConfig, ch_client):
    """A derived schema selecting five of twelve columns, on a source of its own.

    Separate from ``evolved`` because the selection has to be in place before the
    deploy renders the table: binding one to a source that already has a wider
    table would leave the extra columns behind and 2.5 would read them.
    """
    require(e2e, "receiver_url", "ch_host")
    _delete(engine, NARROW_SOURCE)
    drop_table(ch_client, e2e.ch_db, NARROW_SOURCE)
    engine.call("DELETE", f"/schemas/definitions/derived/{DERIVED_PATH}")

    select = [
        {"name": name} if index is None else {"name": name, "index": index}
        for name, index in DERIVED_SELECT.items()
    ]
    written = engine.call(
        "POST",
        f"/schemas/definitions/derived/{DERIVED_PATH}",
        {
            "base": SHIPPED_SCHEMA,
            "base_version": SHIPPED_SCHEMA_VERSION,
            "current": DERIVED_VERSION,
            "versions": {
                DERIVED_VERSION: {
                    "date": "2026-09-23",
                    "summary": "Acceptance 2.4 -- five of twelve, three re-indexed",
                    "select": select,
                }
            },
        },
    )
    assert written.status_code in (200, 201), (
        f"the deployment refused the derived schema: {written.status_code} {written.text}"
    )
    created = engine.call(
        "POST",
        "/sources",
        {
            "source": NARROW_SOURCE,
            "display_name": "Derived schema acceptance",
            "description": "A source whose table is narrowed by a derived schema.",
            "match": {"field": MATCH_FIELD, "operator": "equals", "value": NARROW_SOURCE},
            "header": HEADER,
            "schema": {
                "meta_schema": SHIPPED_SCHEMA,
                "meta_schema_version": SHIPPED_SCHEMA_VERSION,
                "derived_schema": DERIVED_PATH,
                "derived_schema_version": DERIVED_VERSION,
            },
        },
    )
    assert created.status_code == 201, (
        f"the deployment refused the narrowed source: {created.status_code} {created.text}"
    )
    _deploy(engine, NARROW_SOURCE)
    yield NARROW_SOURCE
    _delete(engine, NARROW_SOURCE)
    drop_table(ch_client, e2e.ch_db, NARROW_SOURCE)
    engine.call("DELETE", f"/schemas/definitions/derived/{DERIVED_PATH}")


@pytest.fixture(scope="module")
def cisco_samples() -> list[corpus.Sample]:
    """Step 2.7's input, or the skip that says the corpus is not here."""
    if not corpus.available():
        pytest.skip(f"filebeat corpus not present at {corpus.CORPUS}")
    found = corpus.samples(modules=(TRANSFORM_MODULE,), limit=10)
    if not found:
        pytest.skip(f"the filebeat corpus carries no {TRANSFORM_MODULE} samples")
    return found


@pytest.fixture(scope="module")
def transformed_table(
    engine: EngineAPI, e2e: E2EConfig, ch_client, cisco_samples: list[corpus.Sample]
):
    """Step 2.7's source: its own table, with a transform in front of it.

    Two things a deploy does NOT do, and 2.7 cannot do for itself: start the
    per-source transform instance, and make the receiver pick up routing for a
    transform-bearing source - which it needs a restart for, while the API
    reports ``restart_required: []``.
    """
    require(e2e, "receiver_url", "ch_host")
    _delete(engine, TRANSFORM_SOURCE)
    drop_table(ch_client, e2e.ch_db, TRANSFORM_SOURCE)
    created = engine.call(
        "POST",
        "/sources",
        {
            "source": TRANSFORM_SOURCE,
            "display_name": "Data evolution acceptance - transformed",
            "description": "A syslog line in `message` parsed into typed columns.",
            # The corpus wrapper stamps `_source`, and a source declaring this
            # match compiles to exactly that rule at the receiver.
            "match": {"field": "_source", "operator": "equals", "value": TRANSFORM_SOURCE},
            "header": HEADER,
            "schema": {"meta_schema": SHIPPED_SCHEMA},
            "transform": {"engine": _transform_engine(e2e)},
        },
    )
    assert created.status_code == 201, (
        f"the deployment refused the transformed source: {created.status_code} {created.text}"
    )
    deployed = _deploy(engine, TRANSFORM_SOURCE)
    assert deployed["applied"], f"the transformed source did not deploy: {deployed}"
    yield f"{e2e.ch_db}.{TRANSFORM_SOURCE}"
    _delete(engine, TRANSFORM_SOURCE)
    drop_table(ch_client, e2e.ch_db, TRANSFORM_SOURCE)


# --- Stage 0, it just works -------------------------------------------------


class TestStage0:
    """Data lands with nothing declared, and the blob is queryable."""

    def test_0_1_a_record_with_no_schema_or_source_lands_in_main(self, e2e, ch_client) -> None:
        require(e2e, "receiver_url", "ch_host")
        probe = f"ribbit{uuid.uuid4().hex}"
        post_events(e2e, [{"message": f"ground zero {probe}", "fred": {"nerk": {"frog": probe}}}])

        landed = poll_until(
            lambda: count_rows(ch_client, f"{e2e.ch_db}.{LANDING_TABLE}", contains=probe),
            timeout=LANDING_DEADLINE,
            desc=f"the undeclared record in {e2e.ch_db}.{LANDING_TABLE}",
        )
        assert landed > 0

    def test_0_2_a_nested_sub_field_answers_a_query(self, e2e, ch_client) -> None:
        """Found BY the nested path, not found by ``_raw`` and then read.

        Without this the suite has tested insertion, not usefulness: a row whose
        ``_json`` cannot be reached by ``fred.nerk.frog`` still satisfies 0.1.
        """
        require(e2e, "receiver_url", "ch_host")
        probe = f"ribbit{uuid.uuid4().hex}"
        post_events(e2e, [{"message": "ground zero nested", "fred": {"nerk": {"frog": probe}}}])

        table = f"{e2e.ch_db}.{LANDING_TABLE}"
        found = poll_until(
            lambda: _scalar(
                ch_client,
                f"SELECT JSONExtractString(_json, 'fred', 'nerk', 'frog') FROM {table} "
                "WHERE JSONExtractString(_json, 'fred', 'nerk', 'frog') = %(p)s",
                {"p": probe},
            ),
            timeout=LANDING_DEADLINE,
            desc=f"fred.nerk.frog to answer for {probe} in {table}",
        )
        assert found == probe


# --- Stage 1, a meta schema -------------------------------------------------


class TestStage1:
    """A schema arrives pre-supplied, and a field grows into one."""

    def test_1_1_a_pre_supplied_meta_schema_ships_read_only(self, engine: EngineAPI) -> None:
        # per_page=-1 returns every item in one page, so the assertion below does
        # not depend on how many schemas this deployment ships.
        listed = engine.json("GET", "/schemas?per_page=-1")["items"]
        by_name = {str(s["name"]): s for s in listed}
        assert SHIPPED_SCHEMA in by_name, (
            f"{SHIPPED_SCHEMA} is not shipped; this deployment carries {sorted(by_name)[:8]}"
        )
        entry = by_name[SHIPPED_SCHEMA]
        assert entry["resource_type"] == "core", (
            f"{SHIPPED_SCHEMA} is {entry['resource_type']!r}, so it was authored here rather "
            "than supplied by dfe-schemas"
        )

        version = str(entry["current"])
        columns_path = f"/schemas/definitions/{SHIPPED_SCHEMA}/versions/columns"
        definition = engine.json("GET", f"{columns_path}?version={version}&per_page=-1")
        columns = {str(c["name"]) for c in definition["version"]["columns"]["items"]}
        assert PARSED_COLUMN in columns, (
            f"{SHIPPED_SCHEMA} {version} names {len(columns)} column(s) and {PARSED_COLUMN} is "
            "not among them, so the shipped schema does not declare what the transform fills"
        )

        # Moving `current` to a version that does not exist is a real mutation,
        # so the 409 is the core guard rather than the validator behind it.
        refused = engine.call(
            "PATCH", f"/schemas/definitions/{SHIPPED_SCHEMA}", {"current": "9.9.9"}
        )
        assert refused.status_code == 409, (
            f"a write to the core schema {SHIPPED_SCHEMA} answered {refused.status_code}, not a "
            f"refusal: {refused.text}"
        )
        assert "Core resources can't be mutated" in refused.text

    def test_1_2_a_meta_schema_exports_then_re_imports(
        self, engine: EngineAPI, exported_schema: dict
    ) -> None:
        """The export has to carry everything the import needs.

        Compared per column on name, type, use case and expr rather than by
        count. ``expr`` is the sharp one: it is written verbatim into the
        ClickHouse COMMENT and read back by dfe-loader, so an import that drops
        it produces a schema that looks right and instructs nothing.
        """
        sent = _column_signatures(exported_schema["versions"][ROUNDTRIP_VERSION]["columns"])
        assert any(signature[3] for signature in sent), (
            "the exported schema carries no expr at all, so this cannot tell whether "
            "the import preserves one"
        )

        landed = engine.call("POST", "/schemas/import", exported_schema)
        assert landed.status_code in (200, 201), (
            f"the re-import was refused: {landed.status_code} {landed.text}"
        )

        columns_path = f"/schemas/definitions/{ROUNDTRIP_TARGET}/versions/columns"
        read = engine.json("GET", f"{columns_path}?version={ROUNDTRIP_VERSION}&per_page=-1")
        got = _column_signatures(read["version"]["columns"]["items"])

        assert len(got) == len(sent), f"{len(sent)} columns exported and {len(got)} imported"
        lost = [signature for signature in sent if signature not in got]
        assert not lost, (
            f"{len(lost)} column(s) did not survive the round trip on "
            f"{', '.join(_COMPARED_FIELDS)}: {lost[:3]}"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "dfe-engine#459: promote writes @copy into the column COMMENT and dfe-loader does "
            "not carry that directive, so the column is created and never fills"
        ),
    )
    def test_1_3_a_field_seen_in_json_promotes_to_a_real_column(
        self, engine: EngineAPI, e2e, ch_client, evolved: Evolved
    ) -> None:
        """The column has to FILL. One that exists and stays empty is the failure.

        A fresh field per run, because a promoted path stops being an unpromoted
        json-path: reusing one makes this pass once and then fail for a reason
        that is not the product's.

        ``main`` is not usable here - it is engine-owned, and a promote on it is
        refused 409 - which is why the step runs against the source above.
        """
        require(e2e, "receiver_url", "ch_host")
        field = f"p{uuid.uuid4().hex[:8]}"
        json_path = f"probe.{field}"
        column = f"probe_{field}"

        def body(value: str) -> dict[str, Any]:
            return {"tags": {"feed": MATCH_VALUE}, "probe": {field: value}}

        seed = f"seed{uuid.uuid4().hex}"
        post_events(e2e, [body(seed)])
        poll_until(
            lambda: _scalar(
                ch_client,
                f"SELECT count() FROM {evolved.table} "
                f"WHERE JSONExtractString(_json, 'probe', '{field}') = %(p)s",
                {"p": seed},
            ),
            timeout=LANDING_DEADLINE,
            desc=f"the pre-promote record in {evolved.table}",
        )

        # Read while `current` is the DEPLOYED version. Once a newer version
        # exists undeployed, discovery falls back to the landing table filtered
        # by the match rule, where this source's records are not, and answers
        # with no paths at all.
        discovered = engine.json("GET", f"/schemas/{evolved.name}/json-paths")
        assert discovered["table"] == evolved.table, (
            f"discovery ran against {discovered['table']} rather than the source's own table, "
            "so the paths it offers are not this source's"
        )
        offered = {str(p["path"]) for p in discovered["paths"]}
        assert json_path in offered, (
            f"{json_path} is in the landed record but json-paths offers {len(offered)} other "
            "path(s), so there is nothing to promote"
        )

        promoted = engine.json(
            "POST",
            f"/schemas/{evolved.name}/promote-field",
            {"json_path": json_path, "column_name": column},
        )
        assert promoted["schema_version"], f"promote created no schema version: {promoted}"

        deployed = _deploy(engine, evolved.name)
        assert deployed["applied"], f"the promoted version did not deploy: {deployed}"
        assert _scalar(
            ch_client,
            "SELECT count() FROM system.columns WHERE database = %(d)s AND table = %(t)s "
            "AND name = %(c)s",
            {"d": e2e.ch_db, "t": evolved.name, "c": column},
        ), f"{column} is absent from {evolved.table} after the deploy"

        # A record per round, because the loader holds this table's directives
        # for DIRECTIVE_CACHE_TTL: a record sent before the turnover can never
        # fill the column however long it is then waited for.
        after = f"after{uuid.uuid4().hex}"

        def a_later_record_fills() -> int:
            post_events(e2e, [body(after)])
            return int(
                _scalar(
                    ch_client,
                    f"SELECT count() FROM {evolved.table} WHERE {column} = %(p)s",
                    {"p": after},
                )
                or 0
            )

        filled = poll_until(
            a_later_record_fills,
            timeout=PROMOTION_DEADLINE,
            interval=30.0,
            desc=(
                f"{column} to carry the value of {json_path} on a record sent after the "
                "promote - the column exists, so an empty one is the promote reaching the "
                "table and not the loader"
            ),
        )
        assert filled > 0


# --- Stage 2, a DFE source --------------------------------------------------


class TestStage2:
    """Its own routing, its own table, typed columns, and an index that moves."""

    def test_2_1_a_source_declares_a_key_equals_value_routing_condition(
        self, evolved: Evolved
    ) -> None:
        match = evolved.version()["match"]
        assert match is not None, "the stored version carries no match rule at all"
        assert (match["field"], match["operator"], match["value"]) == (
            MATCH_FIELD,
            "equals",
            MATCH_VALUE,
        ), f"the source stored {match}, not {MATCH_FIELD} equals {MATCH_VALUE}"

    def test_2_2_deploy_renders_the_ddl_and_creates_the_table(
        self, e2e, ch_client, evolved: Evolved
    ) -> None:
        assert evolved.deployed["applied"], f"the deploy did not apply: {evolved.deployed}"
        assert not evolved.deployed.get("validation_errors"), (
            f"the DDL did not validate: {evolved.deployed['validation_errors']}"
        )
        assert evolved.name in evolved.deployed["create_table"], (
            "the deploy rendered a CREATE TABLE for another table: "
            f"{evolved.deployed['create_table'][:200]}"
        )
        # A failed apps sync never fails the deploy, so unasserted it surfaces
        # two steps later as records in the wrong table.
        assert not evolved.deployed["apps_sync_error"], (
            f"the apps could not follow the source: {evolved.deployed['apps_sync_error']}"
        )
        # Rendered is not created: the assertion is the table on the server.
        assert _scalar(
            ch_client,
            "SELECT count() FROM system.tables WHERE database = %(d)s AND name = %(t)s",
            {"d": e2e.ch_db, "t": evolved.name},
        ), f"the deploy reported applied but {evolved.table} is not there"

    def test_2_3_records_land_in_the_source_table_with_typed_columns_filled(
        self, e2e, ch_client, evolved: Evolved
    ) -> None:
        """The sharp one.

        A source can get its own table and schema and still have every field
        reachable only through ``_json``: count rows and it passes, read the
        typed columns and it does not. So this asserts VALUES, and sends a
        control record that must land somewhere else.
        """
        require(e2e, "receiver_url", "ch_host")
        probe = f"probe{uuid.uuid4().hex}"
        control = f"control{uuid.uuid4().hex}"
        post_events(
            e2e,
            [
                {
                    "tags": {"feed": MATCH_VALUE},
                    "hostname": f"host-{probe}",
                    "appname": "sshd",
                    "facility": "auth",
                    "severity": "info",
                    "message": f"typed column probe {probe}",
                },
                {
                    "tags": {"feed": "somewhere-else"},
                    "hostname": f"host-{control}",
                    "message": f"control record {control}",
                },
            ],
        )

        poll_until(
            lambda: _scalar(
                ch_client,
                f"SELECT count() FROM {evolved.table} WHERE hostname = %(h)s",
                {"h": f"host-{probe}"},
            ),
            timeout=LANDING_DEADLINE,
            desc=f"the matched record in {evolved.table}",
        )
        typed = _row(
            ch_client,
            f"SELECT app_name, facility, severity, message FROM {evolved.table} "
            "WHERE hostname = %(h)s",
            {"h": f"host-{probe}"},
        )
        assert typed is not None
        declared = ("app_name", "facility", "severity", "message")
        empty = [name for name, value in zip(declared, typed, strict=True) if not value]
        assert not empty, (
            f"{', '.join(empty)} on {evolved.table} exist and are EMPTY, so those fields "
            "reached the table only inside _json and the meta schema bought nothing"
        )

        # The control record has to land somewhere before its absence here says
        # anything, because until then zero only means it is still in flight.
        poll_until(
            lambda: count_rows(ch_client, f"{e2e.ch_db}.{LANDING_TABLE}", contains=control),
            timeout=LANDING_DEADLINE,
            desc=f"the control record in {e2e.ch_db}.{LANDING_TABLE}",
        )
        strayed = _scalar(
            ch_client,
            f"SELECT count() FROM {evolved.table} WHERE hostname = %(h)s",
            {"h": f"host-{control}"},
        )
        assert not strayed, (
            f"the control record reached {evolved.table} as well, so {MATCH_FIELD} is not "
            "discriminating and the source's table holds another feed's records"
        )

    def test_2_4_a_derived_schema_selects_a_subset_with_an_index_per_field(
        self, engine: EngineAPI, narrowed: str
    ) -> None:
        """The selection has to survive the round trip, index answers included.

        A store that keeps the column list and loses the per-field index would
        pass a column count and fail the step, so the assertion is per column
        rather than on the size of the selection.
        """
        read = engine.json("GET", f"/schemas/definitions/derived/{DERIVED_PATH}")
        assert (read["base"], read["base_version"]) == (
            SHIPPED_SCHEMA,
            SHIPPED_SCHEMA_VERSION,
        ), f"the derived schema reads back against {read['base']}@{read['base_version']}"

        stored = {
            entry["name"]: entry.get("index")
            for entry in read["versions"][read["current"]]["select"]
        }
        assert set(stored) == set(DERIVED_SELECT), (
            f"the selection reads back as {sorted(stored)}, not {sorted(DERIVED_SELECT)}"
        )
        overridden = {name: index for name, index in DERIVED_SELECT.items() if index is not None}
        wrong = {name: stored[name] for name, index in overridden.items() if stored[name] != index}
        assert not wrong, (
            f"the per-field index did not survive the write: {wrong}, sent {overridden}"
        )

    def test_2_5_deploying_it_routes_the_feed_to_the_narrower_table(
        self, e2e, ch_client, narrowed: str
    ) -> None:
        """The absent columns are the assertion, not the present ones.

        A table carrying all twelve and filling five would pass a row count and a
        value check and still be the wrong table, so this reads the seven the
        selection drops and requires them to be missing.
        """
        require(e2e, "receiver_url", "ch_host")
        table = f"{e2e.ch_db}.{narrowed}"
        columns = [
            str(row[0])
            for row in ch_client.query(
                "SELECT name FROM system.columns "
                "WHERE database = %(d)s AND table = %(t)s ORDER BY position",
                parameters={"d": e2e.ch_db, "t": narrowed},
            ).result_rows
        ]
        assert columns, f"the deploy created no {table}"

        # The common header is the engine's, not the selection's, and every
        # column it contributes is underscore-prefixed.
        body = [name for name in columns if not name.startswith("_")]
        assert body == list(DERIVED_SELECT), (
            f"{table} carries {body}, not the {list(DERIVED_SELECT)} the derived schema selects"
        )
        assert len(columns) > len(body), (
            f"the common header did not survive the selection: {table} is only {body}"
        )
        strayed = [name for name in DERIVED_DROPPED if name in columns]
        assert not strayed, (
            f"{table} still carries {strayed}, so the derived schema narrowed the "
            "definition and not the deployed table"
        )

        probe = f"narrow{uuid.uuid4().hex}"
        record = {
            "tags": {"feed": narrowed},
            "@timestamp": "2026-09-23T04:05:06.000Z",
            "host": {"name": f"host-{probe}"},
            "agent": {"type": "filebeat", "version": "8.0.0"},
            "event": {"module": "system", "dataset": "system.auth"},
            "log": {"file": {"path": "/var/log/auth.log"}},
            "source": {"ip": "203.0.113.9"},
            "user": {"name": "acceptance"},
            "process": {"name": "sshd", "pid": 4242},
            "message": f"narrow probe {probe}",
        }

        def _sent_and_landed() -> Any:
            # A record posted before the receiver reloads the deploy's routing is
            # routed to the landing table and never reaches this one, so one
            # record sent once cannot prove the step either way.
            post_events(e2e, [record])
            return _scalar(
                ch_client,
                f"SELECT count() FROM {table} WHERE host_name = %(h)s",
                {"h": f"host-{probe}"},
            )

        poll_until(
            _sent_and_landed,
            timeout=LANDING_DEADLINE,
            desc=f"a record in {table}",
        )
        typed = _row(
            ch_client,
            f"SELECT event_dataset, toString(source_ip), message FROM {table} "
            "WHERE host_name = %(h)s",
            {"h": f"host-{probe}"},
        )
        assert typed is not None
        empty = [
            name
            for name, value in zip(("event_dataset", "source_ip", "message"), typed, strict=True)
            if not value
        ]
        assert not empty, (
            f"{', '.join(empty)} on {table} exist and are EMPTY, so the record reached "
            "the narrower table without its selected columns filling"
        )

    def test_2_6_an_index_is_added_and_dropped_on_the_live_table(
        self, e2e, ch_client, evolved: Evolved
    ) -> None:
        """Changing which columns carry an index is an ALTER, not a rebuild.

        ``system.data_skipping_indices`` is what the reconcile reads back, so the
        assertion is against the LIVE set rather than against the DDL we sent. A
        rebuild would replace every active part, which is what the part count
        catches.

        This applies the ALTER directly rather than through the derived-schema
        CRUD that emits it, which is steps 2.4 and 2.5.
        """
        index = f"idx_{evolved.name}_hostname"
        where = "WHERE database = %(d)s AND table = %(t)s"
        args = {"d": e2e.ch_db, "t": evolved.name}

        def live_indexes() -> set[str]:
            rows = ch_client.query(
                f"SELECT name FROM system.data_skipping_indices {where}", parameters=args
            ).result_rows
            return {str(r[0]) for r in rows}

        def rows_and_parts() -> tuple[int, int]:
            return (
                int(_scalar(ch_client, f"SELECT count() FROM {evolved.table}") or 0),
                int(
                    _scalar(
                        ch_client,
                        f"SELECT count() FROM system.parts {where} AND active",
                        args,
                    )
                    or 0
                ),
            )

        assert index not in live_indexes(), f"{index} is already on {evolved.table}"
        before = rows_and_parts()
        assert before[0] > 0, (
            f"{evolved.table} is empty, so nothing here could be rebuilt and the step proves "
            "nothing - 2.3 is what puts records in it"
        )

        _alter(
            ch_client,
            f"ALTER TABLE {evolved.table}{{on_cluster}} ADD INDEX {index} hostname "
            "TYPE set(0) GRANULARITY 4",
        )
        assert index in live_indexes(), (
            f"the ALTER returned but {index} is not in system.data_skipping_indices, so the "
            "reconcile would read the table as still needing it"
        )
        added = rows_and_parts()
        assert added == before, (
            f"ADD INDEX moved {evolved.table} from {before[0]} row(s) across {before[1]} active "
            f"part(s) to {added}, so the data was rewritten rather than the metadata"
        )

        _alter(ch_client, f"ALTER TABLE {evolved.table}{{on_cluster}} DROP INDEX {index}")
        assert index not in live_indexes(), f"{index} survived the DROP"
        dropped = rows_and_parts()
        assert dropped == before, (
            f"the add-and-drop cycle left {evolved.table} at {dropped} rather than the {before} "
            "it started at"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "dfe-loader#184: the transform produces the ECS record on the source's load topic "
            "and the loader never lands it, so the source's table stays empty"
        ),
    )
    def test_2_7_a_transform_turns_a_text_line_into_typed_columns(
        self, e2e, ch_client, transformed_table: str, cisco_samples: list[corpus.Sample]
    ) -> None:
        """A syslog line in ``message`` becomes indexed columns.

        Real upstream samples from elastic/integrations at pin c7bc5302, read out
        of the archive in memory because the data is Elastic-licensed.

        Counted before and after rather than matched on a marker: the event's
        identity is spent producing typed columns, and the transform stores the
        parsed body rather than the line that carried it.
        """
        require(e2e, "receiver_url", "ch_host")
        # The zero values are excluded because a non-nullable column carries one
        # on every row, which would count an untransformed row as a parsed one.
        parsed = f"toString({PARSED_COLUMN}) NOT IN ('', '::', '0.0.0.0')"
        before = count_rows(ch_client, transformed_table, where=parsed)
        post_events(
            e2e, corpus.wrap_all(cisco_samples, source=TRANSFORM_SOURCE, run=uuid.uuid4().hex)
        )

        gained = poll_until(
            lambda: count_rows(ch_client, transformed_table, where=parsed) - before,
            timeout=LANDING_DEADLINE,
            desc=(
                f"a row in {transformed_table} carrying {PARSED_COLUMN}, which the corpus line "
                "has only inside its text and only the transform pulls out - check the "
                "per-source transform instance is running and the receiver has been restarted "
                "onto this source's routing before reading a timeout as the product's fault"
            ),
        )
        assert gained > 0


class TestStage3:
    """Population of `_json` and `_raw` stops, and starts again.

    The decision has to be REVERSIBLE, so the columns stay on the table either
    way and only the writing stops. `capture_json: false` with `capture_raw:
    false` compiles to dfe-loader's `extracted_only`, and json alone to its
    `json_only`.
    """

    def test_3_1_a_derived_schema_stops_population_and_keeps_the_columns(
        self, e2e, ch_client, capture_off: str
    ) -> None:
        """Dropping the columns would be the wrong implementation of stopping.

        The common header is what the rest of DFE reads, so a table that lost
        `_json` could not be switched back without a migration -- which is the
        opposite of the reversible decision stage 3 is for.
        """
        columns = {
            str(row[0])
            for row in ch_client.query(
                "SELECT name FROM system.columns WHERE database = %(d)s AND table = %(t)s",
                parameters={"d": e2e.ch_db, "t": capture_off},
            ).result_rows
        }
        assert columns, f"the deploy created no {e2e.ch_db}.{capture_off}"
        missing = [name for name in ("_json", "_raw") if name not in columns]
        assert not missing, (
            f"{', '.join(missing)} was DROPPED from {e2e.ch_db}.{capture_off} rather than "
            "left unpopulated, so turning capture back on would need a migration"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "dfe-engine#513: the deploy writes table_capture_modes and reports "
            "restart_required: [], but dfe-loader logs no config reload, so the mode "
            "only takes effect after the loader is restarted by hand"
        ),
    )
    def test_3_2_the_feed_lands_typed_with_json_empty(
        self, e2e, ch_client, capture_off: str
    ) -> None:
        require(e2e, "receiver_url", "ch_host")
        table = f"{e2e.ch_db}.{capture_off}"
        probe = f"capture{uuid.uuid4().hex}"
        record = {
            "tags": {"feed": capture_off},
            "@timestamp": "2026-09-23T04:05:06.000Z",
            "host": {"name": f"host-{probe}"},
            "event": {"dataset": "system.auth"},
            "message": f"capture probe {probe}",
        }

        def _sent_and_landed() -> Any:
            post_events(e2e, [record])
            return _scalar(
                ch_client,
                f"SELECT count() FROM {table} WHERE host_name = %(h)s",
                {"h": f"host-{probe}"},
            )

        poll_until(_sent_and_landed, timeout=LANDING_DEADLINE, desc=f"a record in {table}")
        typed, json_length = _row(
            ch_client,
            f"SELECT message, length(toString(`_json`)) FROM {table} "
            "WHERE host_name = %(h)s LIMIT 1",
            {"h": f"host-{probe}"},
        )
        assert typed, "the typed column did not fill, so this says nothing about _json"
        assert json_length == EMPTY_JSON_LENGTH, (
            f"_json on {table} renders {json_length} characters, not the "
            f"{EMPTY_JSON_LENGTH} of an empty object, so population did not stop"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "dfe-engine#513: turning capture back on writes the loader config and needs "
            "the same manual restart, so a new record still lands with _json empty"
        ),
    )
    def test_3_3_turning_population_back_on_refills_json(
        self, engine: EngineAPI, e2e, ch_client, capture_off: str
    ) -> None:
        """The reverse direction, which is the half that makes it a decision.

        Asserts the OFF state first. A record landing with `_json` filled after
        the switch proves nothing on its own -- the loader's default mode is
        `full`, so a run where population never stopped looks identical.

        Rewrites the SAME derived version rather than adding one: the source pins
        `derived_schema_version`, and replacing the versions map out from under
        that pin is dfe-engine#519.
        """
        require(e2e, "receiver_url", "ch_host")
        table = f"{e2e.ch_db}.{capture_off}"
        before = f"stopped{uuid.uuid4().hex}"
        stopped_record = {
            "tags": {"feed": capture_off},
            "@timestamp": "2026-09-23T04:05:06.000Z",
            "host": {"name": f"host-{before}"},
            "event": {"dataset": "system.auth"},
            "message": f"stopped probe {before}",
        }

        def _sent_while_off() -> Any:
            post_events(e2e, [stopped_record])
            return _scalar(
                ch_client,
                f"SELECT count() FROM {table} WHERE host_name = %(h)s",
                {"h": f"host-{before}"},
            )

        poll_until(_sent_while_off, timeout=LANDING_DEADLINE, desc=f"a record in {table}")
        while_off = _scalar(
            ch_client,
            f"SELECT length(toString(`_json`)) FROM {table} WHERE host_name = %(h)s LIMIT 1",
            {"h": f"host-{before}"},
        )
        assert while_off == EMPTY_JSON_LENGTH, (
            f"_json renders {while_off} characters while population is OFF, not the "
            f"{EMPTY_JSON_LENGTH} of an empty object, so refilling it afterwards would "
            "prove nothing -- the loader is in its default full mode"
        )

        _write_capture_version(engine, capture_json=True)
        _deploy(engine, capture_off)

        probe = f"refill{uuid.uuid4().hex}"
        record = {
            "tags": {"feed": capture_off},
            "@timestamp": "2026-09-23T04:05:06.000Z",
            "host": {"name": f"host-{probe}"},
            "event": {"dataset": "system.auth"},
            "message": f"refill probe {probe}",
        }

        def _sent_and_refilled() -> Any:
            post_events(e2e, [record])
            return _scalar(
                ch_client,
                f"SELECT length(toString(`_json`)) FROM {table} WHERE host_name = %(h)s LIMIT 1",
                {"h": f"host-{probe}"},
            )

        # `done` rather than truthiness: an unpopulated `_json` already renders 2
        # characters, so a truthy check would return on the first poll and read the
        # empty object as a refill.
        filled = poll_until(
            _sent_and_refilled,
            timeout=LANDING_DEADLINE,
            done=lambda length: (length or 0) > EMPTY_JSON_LENGTH,
            desc=f"a record in {table} carrying _json again",
        )
        assert filled > EMPTY_JSON_LENGTH, (
            f"_json on {table} renders {filled} characters for a record sent after the "
            f"switch, still the empty object, so population did not resume"
        )


class TestStage2Swap:
    """Step 2.8 -- the transform is swapped and the same columns still fill.

    2.7 proves ONE transform turns a Cisco IOS line into typed columns. 2.8 asks
    whether a different engine on the same corpus produces the same answer, which
    is what makes the transform a swappable part rather than the schema's owner.
    """

    def test_2_8_a_second_transform_fills_the_same_columns(
        self, e2e, ch_client, swapped_table: tuple, cisco_samples: list[corpus.Sample]
    ) -> None:
        """Counted, never marked.

        The VRL pipeline REPLACES the inbound `_tags` with its own, so a marker
        sent in tags does not survive it and a marker query reads as total loss.
        The parsed column is what says a line was read, so the assertion is the
        gain in rows carrying one.
        """
        require(e2e, "receiver_url", "ch_host")
        table, match = swapped_table
        parsed = f"toString({PARSED_COLUMN}) NOT IN ('', '::', '0.0.0.0')"
        before = count_rows(ch_client, table, where=parsed)
        routed = _nested(match["field"], match["value"])
        post_events(
            e2e,
            [{**routed, "message": sample.line} for sample in cisco_samples],
        )

        gained = poll_until(
            lambda: count_rows(ch_client, table, where=parsed) - before,
            timeout=LANDING_DEADLINE,
            desc=(
                f"a row in {table} carrying {PARSED_COLUMN}, which the corpus line "
                "holds only inside its text - check the per-source transform instance for "
                f"{SWAP_SOURCE} is running before reading a timeout as the product's fault"
            ),
        )
        assert gained > 0
