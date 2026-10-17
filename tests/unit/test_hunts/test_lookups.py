#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_lookups.py
#  Purpose:      The engine's lookup-database check when ClickHouse does not answer
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The engine starts with the lookup check unrun, and says so, when ClickHouse is down."""

from dfe_engine.hunts import lookups


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict]] = []

    def warning(self, message: str, **fields) -> None:
        self.warnings.append((message, fields))


def test_the_engine_starts_with_the_check_unrun_when_clickhouse_does_not_answer(monkeypatch):
    recorded = _Logger()
    monkeypatch.setattr(lookups, "logger", recorded)

    def _down() -> None:
        raise ConnectionError("refused")

    lookups.refuse_tenant_lookups_if_reachable(_down, ["intel"])

    [(message, fields)] = recorded.warnings
    assert message.startswith("lookup databases not checked")
    assert fields["databases"] == ["intel"]


def test_nothing_listed_connects_to_nothing():
    def _never() -> None:
        raise AssertionError("connected with nothing to check")

    lookups.refuse_tenant_lookups_if_reachable(_never, [])
