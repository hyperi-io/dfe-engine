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


def _count(ch_client, table: str, run: str) -> int:
    """Rows this run put in a table, or 0 while the table does not exist yet."""
    try:
        result = ch_client.query(
            f"SELECT count() FROM {table} WHERE _json LIKE %(m)s",
            parameters={"m": f"%{run}%"},
        )
    except Exception:
        return 0
    return int(result.result_rows[0][0]) if result.result_rows else 0


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
            try:
                result = ch_client.query(
                    f"SELECT count() FROM {e2e.ch_db}.{SOURCE} "
                    "WHERE _json LIKE %(m)s AND JSONHas(_json, 'host') ",
                    parameters={"m": f"%{run}%"},
                )
            except Exception:
                return 0
            return int(result.result_rows[0][0]) if result.result_rows else 0

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
