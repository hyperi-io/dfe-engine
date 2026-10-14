#  Project:      dfe-engine
#  File:         tests/integration/test_hunt_runner_label_columns.py
#  Purpose:      A rule reads its source's columns even where they share a name with a label
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A rule filters the source's own column, whatever the runner names its labels.

The runner stamps every detection with labels named ``rule_id``, ``rule_name``,
``severity``, ``hunt_name``, ``source_table`` and ``matched_uuid``. Security
sources carry columns by those names: GuardDuty and Security Hub both have a
numeric ``severity``. ClickHouse resolves a name in ``WHERE`` to a ``SELECT``
alias before a column, so a rule in the same scope as the labels read the label.

The rule and the hunt are written the way the API writes them, the source is the
real landing table plus the vendor's columns, and the detections land in the
real detection table.
"""

import time
import uuid

import pytest
from common.hunt_files import write_hunt, write_rule

from dfe_engine.hunt_runner import ChCoordinator, HuntRunner, HuntWorker, load_specs
from dfe_engine.hunt_runner.spread import current_fire

_INTERVAL = 60  # "* * * * *", the cron write_hunt gives every hunt
_SOURCE = "aws-guardduty"
_NIL_UUID = "00000000-0000-0000-0000-000000000000"

# One rule per runner label, each on the source column of that name. Every rule matches
# the hit row and not the miss row, so the label's own literal could match neither.
LABEL_RULES = {
    "severity": "severity >= 7",
    "rule_id": "rule_id = 'hit'",
    "rule_name": "rule_name = 'hit'",
    "hunt_name": "hunt_name = 'hit'",
    "source_table": "source_table = 'hit'",
    "matched_uuid": "matched_uuid = 'hit'",
}

_LABEL_COLUMNS = (
    "severity UInt8",
    *(f"{name} String" for name in LABEL_RULES if name != "severity"),
)


def _source(ch_client, db: str, columns: tuple[str, ...]) -> None:
    """The real landing table's shape, plus the vendor columns given."""
    ch_client.command(f"CREATE TABLE `{db}`.`{_SOURCE}` AS `{db}`.`main`")
    added = ", ".join(f"ADD COLUMN {column}" for column in columns)
    ch_client.command(f"ALTER TABLE `{db}`.`{_SOURCE}` {added}")


def _land(ch_client, db: str, at: int, row_uuid: str, *, hit: bool) -> None:
    """One finding at epoch second *at*, each label-named column reading hit or miss."""
    text = "hit" if hit else "miss"
    names = ", ".join(LABEL_RULES)
    values = ", ".join(["8" if hit else "3"] + [f"'{text}'"] * (len(LABEL_RULES) - 1))
    ch_client.command(
        f"INSERT INTO `{db}`.`{_SOURCE}` (_timestamp_load, _timestamp, _org_id, _uuid, _json, "
        f"{names}) VALUES (toDateTime64({at}, 3), toDateTime64({at}, 3), 'acme', "
        f"toUUID('{row_uuid}'), '{{\"finding\":\"{text}\"}}', {values})"
    )


def _tick(ch_client, db: str, specs: dict, fire: int) -> ChCoordinator:
    coord = ChCoordinator(ch_client, database=db, settle_seconds=0.0, sleep=lambda _s: None)
    coord.ensure_schema()
    runner = HuntRunner(coord, HuntWorker(ch_client, coord), specs, cap=8)
    assert runner.tick(fire + 1) == 1
    return coord


@pytest.mark.parametrize("column", list(LABEL_RULES))
def test_a_rule_reads_the_source_column_that_shares_a_labels_name(
    ch_client, dfe_db, tmp_path, column
):
    _source(ch_client, dfe_db, _LABEL_COLUMNS)
    hunt = f"label_{uuid.uuid4().hex[:8]}"
    rule_id = f"{hunt}_rule"
    rule_name = f"GuardDuty {column}"
    write_rule(tmp_path / "rules", rule_id, LABEL_RULES[column], name=rule_name, severity="high")
    write_hunt(tmp_path / "hunts", hunt, rule_id, dfe_db, source=_SOURCE)
    fire = current_fire(hunt, _INTERVAL, int(time.time()))
    hit = str(uuid.uuid4())
    _land(ch_client, dfe_db, fire - 5, hit, hit=True)
    _land(ch_client, dfe_db, fire - 10, str(uuid.uuid4()), hit=False)
    # A hit outside the window, which the rule's subquery must still leave out.
    _land(ch_client, dfe_db, fire - 2 * _INTERVAL, str(uuid.uuid4()), hit=True)

    specs = load_specs(tmp_path / "hunts", rules_dir=tmp_path / "rules")
    coord = _tick(ch_client, dfe_db, specs, fire)

    rows = ch_client.query(
        "SELECT toString(matched_uuid), rule_id, rule_name, severity, hunt_name, source_table "
        f"FROM `{dfe_db}`.detection WHERE hunt_name = {{h:String}}",
        parameters={"h": hunt},
    ).result_rows
    assert [tuple(row) for row in rows] == [(hit, rule_id, rule_name, "high", hunt, _SOURCE)]
    assert coord.get_watermark(hunt) == fire


def test_a_capped_rule_counts_on_a_column_named_like_the_counts_own_alias(
    ch_client, dfe_db, tmp_path
):
    """The cap's count runs the rule again beside aliases of its own, dfe_matched among them."""
    _source(ch_client, dfe_db, ("severity UInt8", "dfe_matched UInt8"))
    hunt = f"label_{uuid.uuid4().hex[:8]}"
    rule_id = f"{hunt}_rule"
    write_rule(tmp_path / "rules", rule_id, "severity >= 7 AND dfe_matched = 1")
    write_hunt(tmp_path / "hunts", hunt, rule_id, dfe_db, source=_SOURCE)
    fire = current_fire(hunt, _INTERVAL, int(time.time()))
    # Five matches, then three rows the dfe_matched column rules out.
    ch_client.command(
        f"INSERT INTO `{dfe_db}`.`{_SOURCE}` (_timestamp_load, _timestamp, _org_id, _json, "
        f"severity, dfe_matched) SELECT toDateTime64({fire - 5} - number, 3) AS at, at, 'acme', "
        "'{}', 8, number < 5 FROM numbers(8)"
    )

    specs = load_specs(tmp_path / "hunts", rules_dir=tmp_path / "rules", max_detections=2)
    _tick(ch_client, dfe_db, specs, fire)

    detections, matched = ch_client.query(
        "SELECT countIf(matched_uuid != toUUID({nil:String})), "
        "anyIf(JSONExtractUInt(toJSONString(_json), 'matched'), "
        "matched_uuid = toUUID({nil:String})) "
        f"FROM `{dfe_db}`.detection WHERE hunt_name = {{h:String}}",
        parameters={"h": hunt, "nil": _NIL_UUID},
    ).result_rows[0]
    assert (int(detections), int(matched)) == (2, 5)
