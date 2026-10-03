#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_hunt_hyphenated_source_validation.py
#  Purpose:      HuntValidator accepts a source named for a hyphenated label, never a target
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A source table is named for its source label, so validation lets ``-`` through.

``Source.table_name`` is the label itself, a DNS-1123 label such as ``cisco-ios``.
The runner backtick-quotes both parts into ``FROM``. A results table is held to
letters, digits and ``_``, as before.
"""

from pathlib import Path

import pytest

from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleRegistry
from dfe_engine.hunts.validator import HuntValidator


def _hunt(**overrides) -> dict:
    hunt = {
        "log_buffer": 60,
        "global_source_table_name": "dfe.main",
        "customers": ["org_a"],
        "rules": [{"rule_name": "certutil", "initial_checkpoint_lookback_minutes": 10}],
    }
    hunt.update(overrides)
    return hunt


def _validate(rules_dir: Path, hunt: dict) -> None:
    HuntValidator.validate_hunt_configuration(hunt, rules_dir, "_timestamp_load")


@pytest.mark.parametrize("source", ["dfe.cisco-ios", "cisco-ios", "tenant-a.windows-audit_sigma"])
def test_a_hyphenated_hunt_source_validates(tmp_path: Path, source):
    _validate(tmp_path, _hunt(global_source_table_name=source))


def test_a_hyphenated_rule_entry_source_validates(tmp_path: Path):
    rules = [{"rule_name": "certutil", "source_table_name": "dfe.cisco-meraki"}]

    _validate(tmp_path, _hunt(rules=rules))


def test_a_rule_file_with_a_hyphenated_source_validates(tmp_path: Path):
    registry = RuleRegistry(rules_directory=tmp_path, writable=True, refresh_interval=0)
    try:
        registry.save(
            Rule(
                rule_id="certutil",
                name="Certutil",
                where_clause="a = 1",
                source_db="dfe",
                source_table="cisco-ios",
            )
        )
    finally:
        registry.close()

    _validate(tmp_path, _hunt())


@pytest.mark.parametrize("field", ["global_target_table_name", "target_table_name"])
def test_a_hyphenated_target_is_still_refused(tmp_path: Path, field):
    if field == "global_target_table_name":
        hunt = _hunt(global_target_table_name="dfe.detection-results")
    else:
        hunt = _hunt(rules=[{"rule_name": "certutil", "target_table_name": "dfe.det-results"}])

    with pytest.raises(ValueError, match="is not a table name"):
        _validate(tmp_path, hunt)


def test_a_hyphen_does_not_open_a_source_to_anything_else(tmp_path: Path):
    with pytest.raises(ValueError, match="is not a table name"):
        _validate(tmp_path, _hunt(global_source_table_name="dfe.cisco-ios) -- x"))
