#  Project:      dfe-engine
#  File:         tests/e2e/test_filebeat_pipeline.py
#  Purpose:      The transform layer proved end to end on real filebeat data
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The whole source path, with a transform in it, on real data.

    filebeat samples
      -> dfe-receiver        (JSON conditional routing picks the source)
      -> filebeat_land       (Kafka, source-dedicated topic)
      -> a transform         (the bundled filebeat VRL, or its Vector wrapping)
      -> filebeat_load       (Kafka)
      -> dfe-loader
      -> ClickHouse

Both transform apps run the same bundled VRL, read the same land topic and emit
to the same load topic, and a landed row names neither. So the transform hop is
NOT parameterised over the two: that would be one experiment run twice, and with
both apps deployed each run measures both whatever it is told. DFE_E2E_TRANSFORM
only names the app in the failure message.

Every assertion polls a real signal. The one thing deliberately NOT asserted is
byte-equality against the upstream goldens: the bundled pipeline documents four
known departures from elastic (user_agent wording, query-parameter order,
public-suffix section, ambiguous timezone abbreviations), so equality would fail
for reasons the pipeline's own README already answers. Field presence and the
routing decision are what this proves.

Skipped unless the DFE_E2E_* env is set - see conftest and README-live.md.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.e2e import filebeat_corpus as corpus
from tests.e2e.conftest import E2EConfig, poll_until, require

pytestmark = pytest.mark.live

SOURCE = "filebeat"

# The transform apps this runs against. Exactly one is under test per run, and
# DFE_E2E_TRANSFORM has to name it - see the skip in TestTransform.
TRANSFORMS = ("dfe-transform-vrl", "dfe-transform-vector")

# The filebeat table is built from meta/beats/filebeat.yaml, so ECS lands in
# typed columns rather than in JSON to dig through. log_file_path is derived by
# the transform and absent from the corpus body, so a populated one means the
# event was transformed rather than passed through.
#
# It is the only column of that table this corpus can populate. The bundled VRL
# sets log.file.path once (filebeat.vrl:3020), first thing in the cisco_umbrella
# branch; host.name is set later in that SAME branch (:4095) and so can never
# match a row log_file_path does not, and event.module and event.dataset appear
# nowhere in the file. `message` is excluded because the raw body carries it, and
# `timestamp` because the loader fills it with the arrival time when nothing maps
# to it - a timestamp with a null message is exactly an untransformed row.
#
# So a pass proves the cisco_umbrella branch ran, and says nothing about
# cisco_ios or cisco_meraki: neither sets any column this table has, and both
# could regress to nothing without failing this. Covering them needs the VRL to
# stamp event.module, which the meta schema already declares a column for.
#
# source_ip is the column that SHOULD carry this - the VRL sets source.ip in 39
# places spanning meraki, ios and umbrella. It cannot be used yet because the
# loader rejects an IPv4 literal for an IPv6 column (dfe-loader#127), so every
# row carrying one is dropped before it lands. Switch to it once that is fixed:
# it covers all three modules where log_file_path covers one.
_ECS_POPULATED = "log_file_path != ''"


def _corpus_or_skip(limit: int = 5) -> list[corpus.Sample]:
    if not corpus.available():
        pytest.skip(f"filebeat corpus not present at {corpus.CORPUS}")
    found = corpus.samples(limit=limit)
    if not found:
        pytest.skip("filebeat corpus is empty")
    return found


def _post(cfg: E2EConfig, bodies: list[dict]) -> None:
    # Every caller gates on require(cfg, "receiver_url"), which skips when it is
    # unset; assert so the type reflects that rather than staying str | None.
    assert cfg.receiver_url is not None
    headers = {"Content-Type": "application/json"}
    if cfg.receiver_token:
        headers["Authorization"] = f"Bearer {cfg.receiver_token}"
    with httpx.Client(verify=cfg.verify, timeout=30.0) as client:
        for body in bodies:
            response = client.post(cfg.receiver_url, json=body, headers=headers)
            assert response.status_code < 300, f"receiver rejected the event: {response.text}"


# ClickHouse says one of these when the table or database is simply not there
# yet, which is the only absence a poll should read as "no rows".
_NOT_THERE_YET = ("UNKNOWN_TABLE", "UNKNOWN_DATABASE", "does not exist", "doesn't exist")


def _rows(ch_client, sql: str, run: str) -> int:
    """A counting query's answer, or 0 while its table does not exist yet.

    Only a missing table or database answers 0. Every other error is raised: a
    query that cannot run - an unknown column, a function the column's type will
    not take - otherwise reads as an empty table forever, and a test that can
    only report zero proves nothing.
    """
    try:
        params = {"m": f"%{run}%"} if run else None
        result = ch_client.query(sql, parameters=params)
    except Exception as exc:
        if any(marker in str(exc) for marker in _NOT_THERE_YET):
            return 0
        raise
    return int(result.result_rows[0][0]) if result.result_rows else 0


def _count(ch_client, table: str, run: str) -> int:
    """Rows this run put in a PASSTHROUGH table.

    Matched on ``_raw``, the String (and text-indexed) copy of the payload.
    ``_json`` holds the same bytes but as the ClickHouse JSON type, which LIKE
    and the JSON* string functions both refuse.

    Only the common-header tables carry ``_raw``. A typed source table does not
    - see ``_Delta``.
    """
    return _rows(ch_client, f"SELECT count() FROM {table} WHERE _raw LIKE %(m)s", run)


def _total(ch_client, table: str, where: str = "") -> int:
    """Every row in a table, optionally narrowed."""
    clause = f" WHERE {where}" if where else ""
    return _rows(ch_client, f"SELECT count() FROM {table}{clause}", "")


class _Delta:
    """How many rows a table gained while this ran.

    A typed source table is built only from the columns its meta schema
    declares, so it has no ``_raw``, no ``_tags`` and no run marker to match on:
    the event's identity is spent producing typed columns. The corpus line does
    not survive verbatim either, because the transform stores the PARSED message
    body rather than the syslog line that carried it.

    So the run is isolated by counting before and after instead of by tagging.
    That measures this run's contribution exactly on a quiet deployment, and
    over-counts if something else writes the same table at the same time - which
    can only make the assertion easier to satisfy, never harder, so a pass here
    is worth less than a pass matched on a marker. It is the strongest isolation
    a typed table's own schema permits.
    """

    def __init__(self, ch_client, table: str, where: str = "") -> None:
        self._client = ch_client
        self._table = table
        self._where = where
        self.before = _total(ch_client, table, where)

    def gained(self) -> int:
        return _total(self._client, self._table, self._where) - self.before


class TestRouting:
    """A source-dedicated topic, not the default one."""

    def test_a_discriminated_event_lands_in_its_own_source_table(self, e2e, ch_client) -> None:
        require(e2e, "receiver_url", "ch_host")
        run = f"e2e-{uuid.uuid4().hex}"
        source_table = _Delta(ch_client, f"{e2e.ch_db}.{SOURCE}")
        _post(e2e, corpus.wrap_all(_corpus_or_skip(limit=2), run=run))

        landed = poll_until(
            source_table.gained,
            timeout=180.0,
            desc=f"rows in {e2e.ch_db}.{SOURCE} for {run}",
        )
        assert landed > 0

    def test_an_undiscriminated_event_stays_on_the_default_path(self, e2e, ch_client) -> None:
        # The other half of the routing decision: without the field the rule
        # matches on, an event must NOT reach the source table.
        require(e2e, "receiver_url", "ch_host")
        run = f"e2e-{uuid.uuid4().hex}"
        sample = _corpus_or_skip(limit=1)[0]
        body = corpus.wrap(sample, run=run)
        body.pop("_source")
        source_table = _Delta(ch_client, f"{e2e.ch_db}.{SOURCE}")
        _post(e2e, [body])

        poll_until(
            lambda: _count(ch_client, f"{e2e.ch_db}.default", run),
            timeout=180.0,
            desc=f"rows in {e2e.ch_db}.default for {run}",
        )
        assert source_table.gained() == 0


class TestTransform:
    """The transform actually transformed, and the loader landed the result."""

    def test_ecs_fields_appear_in_the_source_table(self, e2e, ch_client) -> None:
        """The deployed transform transformed, whichever one it is.

        Not parameterised over the two apps. They run the same VRL file, read the
        same land topic and emit to the same load topic, and a row names neither,
        so two parameterisations would be one experiment run twice - and with both
        apps deployed each run measures both regardless of what it is told.
        DFE_E2E_TRANSFORM only NAMES the app for the failure message.
        """
        require(e2e, "receiver_url", "ch_host", "engine_url")
        if e2e.transform is not None and e2e.transform not in TRANSFORMS:
            pytest.fail(
                f"DFE_E2E_TRANSFORM is {e2e.transform!r}, which is not one of "
                f"{', '.join(TRANSFORMS)}"
            )
        deployed = e2e.transform or "the deployed transform"

        run = f"e2e-{uuid.uuid4().hex}"
        parsed = _Delta(ch_client, f"{e2e.ch_db}.{SOURCE}", where=_ECS_POPULATED)
        _post(e2e, corpus.wrap_all(_corpus_or_skip(limit=5), run=run))

        found = poll_until(
            parsed.gained,
            timeout=240.0,
            desc=f"ECS-shaped rows from {deployed} for {run}",
        )
        assert found > 0, (
            f"{deployed} produced no row with log_file_path set - the cisco_umbrella "
            "events reached the table untransformed, which is the pass-through failure "
            "this test exists to catch"
        )


class TestCommonHeaderLowerBound:
    """The path does not depend on a per-source meta schema existing."""

    def test_an_unschemad_source_still_lands(self, e2e, ch_client) -> None:
        require(e2e, "receiver_url", "ch_host")
        run = f"e2e-{uuid.uuid4().hex}"
        unknown = f"harness{uuid.uuid4().hex[:8]}"
        _post(e2e, corpus.wrap_all(_corpus_or_skip(limit=1), source=unknown, run=run))

        # No per-source schema exists for this name, so the common header is what
        # carries it. Landing anywhere at all is the assertion.
        landed = poll_until(
            lambda: (
                _count(ch_client, f"{e2e.ch_db}.{unknown}", run)
                or _count(ch_client, f"{e2e.ch_db}.default", run)
            ),
            timeout=180.0,
            desc=f"rows for the unschema'd source {unknown}",
        )
        assert landed > 0
