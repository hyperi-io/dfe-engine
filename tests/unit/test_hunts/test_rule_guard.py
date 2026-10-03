"""Rule guards: a rule that matches every event, and the bands a measured volume falls in."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from dfe_engine.hunts.rule_guard import (
    VolumeBand,
    VolumeMeasure,
    match_everything,
    preview_settings,
    preview_sql,
    refuse_offbox_calls,
    volume_verdict,
)
from dfe_engine.hunts.rule_rewriter import RuleRewriter
from dfe_engine.settings import DetectionGuardSettings, load_settings

FIXTURE = Path(__file__).parents[2] / "fixtures" / "hdx_sanitizer" / "rendered_views.json"
RENDERED = [c for c in json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"] if "clean_sql" in c]

MATCH_EVERYTHING = [
    "1=1",
    "'a'='a'",
    "x = 1 OR 1=1",
    "1",
    "NOT 0",
    "true",
    "2 > 1",
    "notEmpty(_json) = 1",
    "notEmpty(_json)",
    "(notEmpty(toString(`_json`)) = 1)",
    "_json IS NOT NULL",
    "isNotNull(_json)",
    "_json <> ''",
    "_json != ''",
    "NOT isNull(_uuid)",
    "NOT empty(_json)",
    "empty(_json) = 0",
    "length(_json) > 0",
    "_timestamp IS NOT NULL",
    "notEmpty(assumeNotNull(_json)) != 0",
    "(notEmpty(_json) = 1) AND (_timestamp_load IS NOT NULL)",
    "notEmpty(_json) = 1 AND 1 = 1",
    "notEmpty(_json) = 1 OR severity = 'high'",
    "main._json IS NOT NULL",
]

NARROWS = [
    "_json.a = 1",
    "severity != 'x'",
    "toString(`_json`.`a`) = 'x'",
    "notEmpty(toString(`_json`.`user`.`name`)) = 1",
    "notEmpty(_json) = 1 AND severity = 'high'",
    "_json._uuid IS NOT NULL",
    "hasToken(lower(_json), lower('certutil'))",
    "notEmpty(_json) = 0",
    "NOT (notEmpty(_json) = 1)",
    "_json IS NULL",
    "x = x",
    "0",
    "_source = 'winlogbeat'",
    "notEmpty(_raw) = 1",
    "length(_json) > 5",
]


@pytest.mark.parametrize("where", MATCH_EVERYTHING)
def test_a_condition_that_is_true_for_every_event_is_named(where):
    message = match_everything(where, "dfe.main")

    assert message is not None
    assert message.startswith("This rule matches every event in dfe.main: ")
    assert message.endswith("Add a condition that narrows it.")


@pytest.mark.parametrize("where", NARROWS)
def test_a_condition_that_narrows_is_left_alone(where):
    assert match_everything(where, "dfe.main") is None


# -- a condition that reads outside the row -------------------------------------

# One per family, as the rule author would write it into the detection WHERE.
OFFBOX_CONDITIONS = {
    "url": "url('http://203.0.113.9/leak', 'LineAsString') = 1",
    "s3": "s3('https://203.0.113.9/bucket/key', 'CSV') = 1",
    "file": "file('hostname') LIKE '%a%'",
    "remote": "remote('203.0.113.9', system.users) = 1",
    "dictionary": "dictGet('tenants', 'name', toUInt64(1)) = 'acme'",
    "ai": "aiFilter(toString(_json), 'is it bad') = 1",
    "globalIn": "globalIn(_source, dfe.main)",
    "in": "in(_source, dfe.main)",
    "in after NOT": "severity = 'high' AND NOT in(_source, dfe.main)",
    "nested in a permitted call": "length(aiEmbed(toString(_json))) > 0",
    "beside a real condition": "severity = 'high' AND url('http://203.0.113.9/') = 1",
}


@pytest.mark.parametrize("where", OFFBOX_CONDITIONS.values(), ids=list(OFFBOX_CONDITIONS))
def test_a_condition_that_reads_outside_the_row_is_refused(where):
    message = refuse_offbox_calls(where)

    assert message is not None
    assert message.startswith("A rule's detection condition may not call ")


@pytest.mark.parametrize("where", NARROWS)
def test_a_condition_over_the_row_is_not_refused(where):
    assert refuse_offbox_calls(where) is None


def test_an_empty_condition_has_nothing_to_refuse():
    assert refuse_offbox_calls("  ") is None


@pytest.mark.parametrize(
    "where",
    [
        "_source IN (SELECT ioc FROM threat.iocs)",
        "_source GLOBAL NOT IN (SELECT ioc FROM threat.iocs)",
        "severity IN ('high', 'critical')",
    ],
)
def test_a_condition_using_the_in_operator_is_not_refused(where):
    assert refuse_offbox_calls(where) is None


def test_a_presence_check_names_the_column_every_event_has():
    assert match_everything("notEmpty(_json) = 1", "dfe.main") == (
        "This rule matches every event in dfe.main: its condition is true whenever "
        "_json is present, and every event has it. Add a condition that narrows it."
    )


def test_two_presence_checks_name_both_columns():
    message = match_everything("_json IS NOT NULL AND _uuid IS NOT NULL", "dfe.main")

    assert message is not None
    assert "_json and _uuid are present, and every event has them" in message


def test_a_tautology_says_it_is_always_true():
    assert match_everything("1 = 1", "dfe.main") == (
        "This rule matches every event in dfe.main: its condition is always true. "
        "Add a condition that narrows it."
    )


@pytest.mark.parametrize("where", ["", "   ", "((", "x = = 1"])
def test_an_empty_or_unreadable_condition_is_not_this_guards_call(where):
    assert match_everything(where, "dfe.main") is None


@pytest.mark.parametrize("case", RENDERED, ids=[c["name"] for c in RENDERED])
def test_no_rendered_hyperdx_view_with_a_filter_is_refused(case):
    where = RuleRewriter().parse_user_sql(case["clean_sql"]).where_clause

    assert match_everything(where, "dfe.main") is None


# -- volume bands -------------------------------------------------------------

GUARD = DetectionGuardSettings()


def _day(matched: int, total: int = 1_000_000) -> VolumeMeasure:
    """A measure over a whole day, so the projection is the match count itself."""
    return VolumeMeasure(window_minutes=1440, total=total, matched=matched)


@pytest.mark.parametrize(
    ("matched", "band"),
    [
        (0, VolumeBand.OK),
        (150, VolumeBand.OK),
        (151, VolumeBand.GUIDANCE),
        (300, VolumeBand.GUIDANCE),
        (301, VolumeBand.WARN),
        (10_000, VolumeBand.WARN),
        (10_001, VolumeBand.PLAINLY_BAD),
    ],
)
def test_projected_alerts_a_day_fall_in_the_decided_bands(matched, band):
    got, warnings = volume_verdict(_day(matched), GUARD, "dfe.main")

    assert got is band
    assert bool(warnings) is (band is not VolumeBand.OK)


def test_each_band_says_how_many_alerts_a_day():
    _, guidance = volume_verdict(_day(200), GUARD, "dfe.main")
    _, warn = volume_verdict(_day(5_000), GUARD, "dfe.main")
    _, bad = volume_verdict(_day(20_000), GUARD, "dfe.main")

    assert guidance == [
        "Over the last 1440 minutes this rule matched 200 of 1,000,000 events in "
        "dfe.main, about 200 alerts a day -- more than one analyst typically reviews."
    ]
    assert warn[0].endswith("about 5,000 alerts a day -- consider narrowing it.")
    assert bad[0].endswith(
        "about 20,000 alerts a day -- far more than a team can review. Narrow it before it runs."
    )


def test_the_window_projects_to_a_day():
    measure = VolumeMeasure(window_minutes=60, total=5_000, matched=25)

    assert measure.per_day == 600
    assert volume_verdict(measure, GUARD, "dfe.main")[0] is VolumeBand.WARN


def test_half_the_traffic_over_the_ack_line_is_plainly_bad():
    band, warnings = volume_verdict(_day(600, total=1_000), GUARD, "dfe.main")

    assert band is VolumeBand.PLAINLY_BAD
    assert warnings[1] == (
        "That is 60% of all events in dfe.main. Check the condition narrows it to what you meant."
    )


def test_half_the_traffic_under_the_ack_line_is_a_note_not_a_block():
    # Three days at 200 matches a day: over half the traffic, under the ack line.
    measure = VolumeMeasure(window_minutes=4320, total=1_000, matched=600)

    band, warnings = volume_verdict(measure, GUARD, "dfe.main")

    assert measure.per_day == 200
    assert band is VolumeBand.GUIDANCE
    assert len(warnings) == 2


def test_a_tenth_of_the_traffic_adds_a_note_even_at_a_low_volume():
    band, warnings = volume_verdict(_day(100, total=1_000), GUARD, "dfe.main")

    assert band is VolumeBand.GUIDANCE
    assert warnings == [
        "That is 10% of all events in dfe.main. Check the condition narrows it to what you meant."
    ]


def test_the_share_is_ignored_in_a_thin_window():
    band, warnings = volume_verdict(_day(100, total=999), GUARD, "dfe.main")

    assert band is VolumeBand.OK
    assert warnings == []


def test_an_empty_window_is_unmeasured_not_quiet():
    band, warnings = volume_verdict(VolumeMeasure(60, 0, 0), GUARD, "dfe.main")

    assert band is VolumeBand.UNMEASURED
    assert warnings == [
        "No events reached dfe.main in the last 60 minutes, so this rule's alert "
        "volume could not be measured."
    ]


# -- the preview query ----------------------------------------------------------


def test_the_preview_counts_the_window_and_the_matches_under_its_own_names():
    sql = preview_sql("dfe", "main", "_timestamp_load", "severity = 'high'", 60)

    assert sql == (
        "SELECT count() AS dfe_total, countIf(\n"
        "severity = 'high'\n"
        ") AS dfe_matched\n"
        "FROM `dfe`.`main`\n"
        "WHERE `_timestamp_load` >= now() - INTERVAL 60 MINUTE"
    )


def test_the_preview_throws_at_its_bounds_rather_than_breaking_off():
    settings = preview_settings(DetectionGuardSettings(preview_timeout_seconds=2.5))

    assert settings == {
        "max_execution_time": 2.5,
        "timeout_overflow_mode": "throw",
        "max_rows_to_read": 50_000_000,
        "read_overflow_mode": "throw",
        "readonly": 1,
    }


def test_the_preview_runs_read_only_so_clickhouse_refuses_a_read_through_url():
    assert preview_settings(DetectionGuardSettings())["readonly"] == 1


# -- settings -------------------------------------------------------------------


def test_the_shipped_thresholds_are_the_decided_ones():
    assert GUARD.model_dump() == {
        "enabled": True,
        "preview_window_minutes": 60,
        "preview_timeout_seconds": 10.0,
        "preview_max_rows": 50_000_000,
        "warn_per_day": 150,
        "ack_per_day": 300,
        "block_per_day": 10_000,
        "warn_match_ratio": 0.10,
        "block_match_ratio": 0.50,
        "min_rows_for_ratio": 1000,
    }


@pytest.mark.parametrize(
    "override",
    [
        {"warn_per_day": 300},
        {"ack_per_day": 20_000},
        {"warn_match_ratio": 0.6},
        {"preview_window_minutes": 0},
    ],
)
def test_bands_out_of_order_fail_at_startup(override):
    with pytest.raises(ValidationError):
        DetectionGuardSettings(**override)


def test_every_threshold_takes_its_env_override(monkeypatch):
    for name, value in {
        "ENABLED": "false",
        "PREVIEW_WINDOW_MINUTES": "30",
        "PREVIEW_TIMEOUT_SECONDS": "2.5",
        "PREVIEW_MAX_ROWS": "1000",
        "WARN_PER_DAY": "10",
        "ACK_PER_DAY": "20",
        "BLOCK_PER_DAY": "30",
        "WARN_MATCH_RATIO": "0.2",
        "BLOCK_MATCH_RATIO": "0.3",
        "MIN_ROWS_FOR_RATIO": "5",
    }.items():
        monkeypatch.setenv(f"DFE_DETECTION_GUARD_{name}", value)
    monkeypatch.setenv("DFE_ENV", "dev")
    monkeypatch.setenv("DFE_AUTH_ENABLED", "false")

    guard = load_settings().hunts.detection_guard

    assert guard.model_dump() == {
        "enabled": False,
        "preview_window_minutes": 30,
        "preview_timeout_seconds": 2.5,
        "preview_max_rows": 1000,
        "warn_per_day": 10,
        "ack_per_day": 20,
        "block_per_day": 30,
        "warn_match_ratio": 0.2,
        "block_match_ratio": 0.3,
        "min_rows_for_ratio": 5,
    }
