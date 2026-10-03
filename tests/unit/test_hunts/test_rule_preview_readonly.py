#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_rule_preview_readonly.py
#  Purpose:      The rule volume preview sends ClickHouse its read-only settings
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The preview runs the rule author's own condition, so the query itself carries readonly=1.

``preview_settings`` is tested on its own. This pins the call site, so a preview
that stopped passing those settings to the client fails here. The client below
answers the parse check and records the preview query it is sent.
"""

from typing import Any

from dfe_engine.hunts.rule_creation_service import RuleCreateRequest, RuleCreationService
from dfe_engine.hunts.rule_guard import VolumeBand, preview_settings
from dfe_engine.settings import DetectionGuardSettings, HuntsSettings


class _Result:
    def __init__(self, rows: list[tuple[int, int]]) -> None:
        self.result_rows = rows


class RecordingClient:
    """Accepts EXPLAIN AST and answers the preview's count, keeping what each query sent."""

    def __init__(self) -> None:
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def command(self, cmd: str, *args: Any, **kwargs: Any) -> str:
        return ""

    def query(self, query: str, *args: Any, **kwargs: Any) -> _Result:
        self.queries.append((query, kwargs))
        return _Result([(2000, 5)])


def _preview(guard: DetectionGuardSettings) -> tuple[RecordingClient, Any]:
    client = RecordingClient()
    service = RuleCreationService(hunts=HuntsSettings(detection_guard=guard), ch_client=client)
    result = service.create_rule(
        RuleCreateRequest(
            name="Certutil",
            user_sql="SELECT * FROM acme.events WHERE process_name = 'certutil.exe'",
        ),
        "certutil",
    )
    return client, result


def test_the_preview_query_is_sent_read_only():
    client, result = _preview(DetectionGuardSettings())

    [(sql, kwargs)] = client.queries
    assert "countIf(" in sql
    assert kwargs["settings"]["readonly"] == 1
    assert result.cost_estimate is not None
    assert result.cost_estimate.band is not VolumeBand.UNMEASURED


def test_the_preview_sends_every_bound_the_guard_configures():
    guard = DetectionGuardSettings(preview_timeout_seconds=2.5, preview_max_rows=1234)

    client, _ = _preview(guard)

    [(_, kwargs)] = client.queries
    assert kwargs["settings"] == preview_settings(guard)
