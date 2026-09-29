#  Project:      dfe-engine
#  File:         tests/integration/test_rule_guards.py
#  Purpose:      Rule guards measured on real ClickHouse: match-everything and alert volume
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The rule guards against the REAL landing table, seeded with a known mix of events.

The last 60 minutes of ``main`` hold 2,000 events: 600 logins, 100 alerts, 10
warnings, 5 rare events and 1,285 noise. Another 500 logins landed three hours
ago, outside every window here. So each rule's count, its projection to a day and
its band are known in advance, and a count that read the old rows would show.

The preview runs through the engine's own resilient ClickHouse client, and one
test drives it the way the API does: ``ch_config`` and the pooled singleton.
"""

import pytest

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.hunts.rule_creation_service import RuleCreateRequest, RuleCreationService
from dfe_engine.hunts.rule_guard import VolumeBand
from dfe_engine.settings import DetectionGuardSettings, HuntsSettings

pytestmark = pytest.mark.integration

_RECENT = {"login": 600, "alert": 100, "warnish": 10, "rare": 5, "noise": 1285}
_TOTAL = sum(_RECENT.values())


def _manager_cfg(ch_params: dict) -> dict:
    return {
        "ch_host": ch_params["host"],
        "ch_port": ch_params["port"],
        "ch_username": ch_params.get("username"),
        "ch_password": ch_params.get("password", ""),
        "ch_secure": ch_params.get("secure", False),
        "ch_verify": False,
    }


def _land(ch_client, db: str, kind: str, count: int, *, hours_ago: int = 0) -> None:
    """Land *count* events of *kind*, spread over 50 minutes ending *hours_ago* back."""
    at = f"now64(3) - INTERVAL {hours_ago} HOUR - toIntervalSecond(number % 3000)"
    ch_client.command(
        f"INSERT INTO `{db}`.`main` (_timestamp_load, _timestamp, _org_id, _json) "
        f"SELECT {at}, {at}, 'acme', '{{\"kind\":\"{kind}\"}}' FROM numbers({count})"
    )


@pytest.fixture
def seeded(ch_client, dfe_db):
    """The landing table holding the known mix, recent and old."""
    for kind, count in _RECENT.items():
        _land(ch_client, dfe_db, kind, count)
    _land(ch_client, dfe_db, "login", 500, hours_ago=3)
    return dfe_db


@pytest.fixture
def manager(ch_params):
    """The engine's resilient ClickHouse manager, pointed at the test server."""
    mgr = ClickHouseManager(_manager_cfg(ch_params))
    try:
        yield mgr
    finally:
        mgr._cleanup()


def _service(manager, **guard) -> RuleCreationService:
    hunts = HuntsSettings(detection_guard=DetectionGuardSettings(**guard))
    return RuleCreationService(hunts=hunts, ch_client=manager.get_clickhouse_client())


def _create(service: RuleCreationService, db: str, where: str, rule_id: str = "r1"):
    sql = f"SELECT _timestamp, _json FROM {db}.main WHERE {where}"
    return service.create_rule(RuleCreateRequest(name=rule_id, user_sql=sql), rule_id)


# -- a rule that matches every event --------------------------------------------

# Shapes ClickHouse accepts on the landing table, each true for every event in it.
_MATCHES_EVERYTHING = [
    "notEmpty(toString(`_json`)) = 1",
    "_json IS NOT NULL",
    "isNotNull(_uuid)",
    "toString(_json) <> ''",
    "_timestamp_load IS NOT NULL",
    "1 = 1",
    "_json.kind = 'login' OR 1 = 1",
]


@pytest.mark.parametrize("where", _MATCHES_EVERYTHING)
def test_a_refused_shape_really_matches_every_event(ch_client, seeded, manager, where):
    total, matched = ch_client.query(
        f"SELECT count(), countIf({where}) FROM `{seeded}`.`main`"
    ).result_rows[0]

    result = _create(_service(manager), seeded, where)

    assert (total, matched) == (_TOTAL + 500, _TOTAL + 500)
    assert result.matches_everything is not None
    assert result.matches_everything.startswith(f"This rule matches every event in {seeded}.main")
    assert result.cost_estimate is None


@pytest.mark.parametrize("where", ["_json.kind = 'login'", "_org_id = 'other'"])
def test_a_near_miss_matches_less_and_is_measured(ch_client, seeded, manager, where):
    total, matched = ch_client.query(
        f"SELECT count(), countIf({where}) FROM `{seeded}`.`main`"
    ).result_rows[0]

    result = _create(_service(manager), seeded, where)

    assert matched < total
    assert result.matches_everything is None
    assert result.cost_estimate is not None
    assert result.cost_estimate.band is not VolumeBand.UNMEASURED


# -- measured alert volume --------------------------------------------------------


def test_a_broad_rule_is_called_plainly_bad_with_its_measured_numbers(ch_params, seeded):
    """Driven the way the API drives it: ch_config and the engine's pooled client."""
    cfg = _manager_cfg(ch_params)
    ClickHouseManager.reset_instance()
    ClickHouseManager.get_instance(cfg)
    try:
        result = RuleCreationService(ch_config=cfg).create_rule(
            RuleCreateRequest(
                name="Every login",
                user_sql=f"SELECT _timestamp, _json FROM {seeded}.main WHERE _json.kind = 'login'",
                estimate_cost=True,
                cost_window_minutes=60,
            ),
            "every_login",
        )
    finally:
        ClickHouseManager.reset_instance()

    estimate = result.cost_estimate
    assert estimate is not None
    assert estimate.estimated_rows == 600
    assert estimate.rows_in_window == _TOTAL
    assert estimate.projected_per_day == 14_400
    assert estimate.band == "plainly_bad"
    source = f"{seeded}.main"
    assert result.rule.warnings == [
        f"Over the last 60 minutes this rule matched 600 of 2,000 events in {source}, "
        "about 14,400 alerts a day -- far more than a team can review. Narrow it before it runs.",
        f"That is 30% of all events in {source}. Check the condition narrows it to what you meant.",
    ]


@pytest.mark.parametrize(
    ("kind", "matched", "per_day", "band", "tail"),
    [
        ("alert", 100, 2_400, VolumeBand.WARN, "consider narrowing it."),
        ("warnish", 10, 240, VolumeBand.GUIDANCE, "more than one analyst typically reviews."),
    ],
)
def test_each_band_carries_the_projected_number(
    seeded, manager, kind, matched, per_day, band, tail
):
    result = _create(_service(manager), seeded, f"_json.kind = '{kind}'")

    assert result.cost_estimate is not None
    assert result.cost_estimate.estimated_rows == matched
    assert result.cost_estimate.projected_per_day == per_day
    assert result.cost_estimate.band is band
    assert result.rule.warnings == [
        f"Over the last 60 minutes this rule matched {matched:,} of 2,000 events in "
        f"{seeded}.main, about {per_day:,} alerts a day -- {tail}"
    ]


def test_a_narrow_rule_gets_no_warning(seeded, manager):
    result = _create(_service(manager), seeded, "_json.kind = 'rare'")

    assert result.cost_estimate is not None
    assert result.cost_estimate.estimated_rows == 5
    assert result.cost_estimate.projected_per_day == 120
    assert result.cost_estimate.band is VolumeBand.OK
    assert result.rule.warnings == []


def test_a_longer_window_reaches_the_older_events(seeded, manager):
    result = _create(_service(manager, preview_window_minutes=240), seeded, "_json.kind = 'login'")

    assert result.cost_estimate is not None
    assert result.cost_estimate.window_minutes == 240
    assert result.cost_estimate.rows_in_window == _TOTAL + 500
    assert result.cost_estimate.estimated_rows == 1100
    assert result.cost_estimate.projected_per_day == 6_600


def test_a_preview_past_its_time_limit_says_it_could_not_finish(seeded, manager):
    result = _create(
        _service(manager, preview_timeout_seconds=0.5),
        seeded,
        "sleepEachRow(0.001) = 0 AND _json.kind = 'login'",
    )

    assert result.cost_estimate is not None
    assert result.cost_estimate.band is VolumeBand.UNMEASURED
    assert result.cost_estimate.estimated_rows is None
    assert result.rule.warnings == [
        "The alert-volume preview could not finish: it ran past its 0.5 second limit on "
        f"{seeded}.main. This rule's alert volume is unknown until it runs."
    ]


def test_a_preview_over_its_row_budget_says_it_could_not_finish(seeded, manager):
    result = _create(_service(manager, preview_max_rows=100), seeded, "_json.kind = 'login'")

    assert result.cost_estimate is not None
    assert result.cost_estimate.band is VolumeBand.UNMEASURED
    assert result.rule.warnings == [
        "The alert-volume preview could not finish: the last 60 minutes of "
        f"{seeded}.main are more than its 100 row limit. This rule's alert volume is "
        "unknown until it runs."
    ]


def test_a_query_clickhouse_refuses_is_named_not_passed(seeded, manager):
    result = _create(_service(manager), seeded, "no_such_column = 1")

    assert result.cost_estimate is not None
    assert result.cost_estimate.band is VolumeBand.UNMEASURED
    [warning] = result.rule.warnings
    assert warning.startswith("The alert-volume preview could not finish: ClickHouse refused it (")
    assert "no_such_column" in warning


def test_an_empty_window_is_unmeasured(dfe_db, manager):
    result = _create(_service(manager), dfe_db, "_json.kind = 'login'")

    assert result.cost_estimate is not None
    assert result.cost_estimate.band is VolumeBand.UNMEASURED
    assert result.cost_estimate.rows_in_window == 0
    assert result.rule.warnings == [
        f"No events reached {dfe_db}.main in the last 60 minutes, so this rule's alert "
        "volume could not be measured."
    ]
