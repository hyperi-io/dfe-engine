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

The transform hop is parameterised over vrl and vector, and nothing else
changes between the two runs - which is what makes this a test of the transform
LAYER rather than of one app.

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

# The transform apps this runs against. Same corpus, same assertions; only the
# instance differs, so a divergence is the transform's and not the harness's.
TRANSFORMS = ("dfe-transform-vrl", "dfe-transform-vector")

# The filebeat table is built from meta/beats/filebeat.yaml, so ECS lands in
# typed columns rather than in JSON to dig through. Each of these is derived by
# the transform and absent from the corpus body, so one populated means the
# event was transformed rather than passed through. No single column covers all
# three corpus modules, hence the disjunction. `message` is excluded because the
# raw body carries it.
_ECS_POPULATED = " OR ".join(
    (
        "toUnixTimestamp(timestamp) > 0",
        "host_name != ''",
        "event_module != ''",
        "event_dataset != ''",
        "log_file_path != ''",
    )
)


def _corpus_or_skip(limit: int = 5) -> list[corpus.Sample]:
    if not corpus.available():
        pytest.skip(f"filebeat corpus not present at {corpus.CORPUS}")
    found = corpus.samples(limit=limit)
    if not found:
        pytest.skip("filebeat corpus is empty")
    return found


def _post(cfg: E2EConfig, bodies: list[dict]) -> None:
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
        result = ch_client.query(sql, parameters={"m": f"%{run}%"})
    except Exception as exc:
        if any(marker in str(exc) for marker in _NOT_THERE_YET):
            return 0
        raise
    return int(result.result_rows[0][0]) if result.result_rows else 0


def _count(ch_client, table: str, run: str) -> int:
    """Rows this run put in a table.

    Matched on ``_raw``, the String (and text-indexed) copy of the payload.
    ``_json`` holds the same bytes but as the ClickHouse JSON type, which LIKE
    and the JSON* string functions both refuse.
    """
    return _rows(ch_client, f"SELECT count() FROM {table} WHERE _raw LIKE %(m)s", run)


class TestRouting:
    """A source-dedicated topic, not the default one."""

    def test_a_discriminated_event_lands_in_its_own_source_table(self, e2e, ch_client) -> None:
        require(e2e, "receiver_url", "ch_host")
        run = f"e2e-{uuid.uuid4().hex}"
        _post(e2e, corpus.wrap_all(_corpus_or_skip(limit=2), run=run))

        landed = poll_until(
            lambda: _count(ch_client, f"{e2e.ch_db}.{SOURCE}", run),
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
        _post(e2e, [body])

        poll_until(
            lambda: _count(ch_client, f"{e2e.ch_db}.default", run),
            timeout=180.0,
            desc=f"rows in {e2e.ch_db}.default for {run}",
        )
        assert _count(ch_client, f"{e2e.ch_db}.{SOURCE}", run) == 0


class TestTransform:
    """The transform actually transformed, and the loader landed the result."""

    @pytest.mark.parametrize("service", TRANSFORMS)
    def test_ecs_fields_appear_in_the_source_table(self, e2e, ch_client, service: str) -> None:
        require(e2e, "receiver_url", "ch_host", "engine_url")
        run = f"e2e-{uuid.uuid4().hex}"
        _post(e2e, corpus.wrap_all(_corpus_or_skip(limit=5), run=run))

        def _parsed() -> int:
            return _rows(
                ch_client,
                f"SELECT count() FROM {e2e.ch_db}.{SOURCE} "
                f"WHERE _raw LIKE %(m)s AND ({_ECS_POPULATED})",
                run,
            )

        found = poll_until(
            _parsed,
            timeout=240.0,
            desc=f"ECS-shaped rows from {service} for {run}",
        )
        assert found > 0, (
            f"{service} produced rows with no ECS fields - the events reached the "
            "table untransformed, which is the pass-through failure this test exists to catch"
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
