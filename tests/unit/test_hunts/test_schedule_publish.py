#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_schedule_publish.py
#  Purpose:      An engine hunt change reaches hunt_schedule, and a failed publish never fails it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The registry tells its change hook about every write that lands, and no other.

The publish itself runs against real ClickHouse in
``tests/integration/test_hunt_schedule_scaler.py``. Here a ClickHouse that cannot be
reached stands in for an outage: the hunt change has to stand.
"""

from collections.abc import Iterator
from pathlib import Path

import clickhouse_connect
import pytest

from dfe_engine.hunts.hunt_config_registry import HuntConfigNotFoundError, HuntConfigRegistry
from dfe_engine.hunts.schedule_publish import StoredHuntSchedule
from dfe_engine.settings import DFESettings

HUNT = {"cron": "*/5 * * * *", "query": "SELECT 1 WHERE {window}"}


@pytest.fixture
def changes() -> list[Path]:
    return []


@pytest.fixture
def registry(tmp_path, changes) -> Iterator[HuntConfigRegistry]:
    found = HuntConfigRegistry(hunts_directory=tmp_path / "hunts", on_change=changes.append)
    yield found
    found.close()


def test_each_kind_of_write_reports_the_hunt_directory_once(registry, changes):
    registry.save("a", HUNT)
    registry.save_many({"a": HUNT, "b": HUNT}, created_by="test", scope="org-x", summary="s")
    registry.delete("a")

    assert changes == [registry.directory] * 3


def test_a_delete_of_a_hunt_that_is_not_there_reports_nothing(registry, changes):
    with pytest.raises(HuntConfigNotFoundError):
        registry.delete("missing")

    assert changes == []


def _unreachable_clickhouse():
    return clickhouse_connect.get_client(host="127.0.0.1", port=1, connect_timeout=1)


def test_an_unreachable_clickhouse_does_not_fail_the_hunt_change(tmp_path):
    schedule = StoredHuntSchedule(
        DFESettings(env="test"), client=_unreachable_clickhouse, rules_dir=tmp_path / "rules"
    )
    registry = HuntConfigRegistry(hunts_directory=tmp_path / "hunts", on_change=schedule.publish)
    try:
        registry.save("a", HUNT)
        assert registry.get("a")["cron"] == HUNT["cron"]
        registry.delete("a")
        assert not registry.exists("a")
    finally:
        registry.close()
