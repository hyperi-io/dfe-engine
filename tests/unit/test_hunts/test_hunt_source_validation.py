#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_hunt_source_validation.py
#  Purpose:      HuntValidator refuses a source that is not a plain table name
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A hunt's source reaches ``FROM`` as text, so validation holds it to a table name.

The hunt API runs this validator on create and update. It reads the hunt's own
source, each rule entry's, and the source stored in each named rule file. The
runner holds the same line at compile, for files committed straight into the
deploy repo. The address below is in the documentation range (RFC 5737).
"""

from pathlib import Path

import pytest

from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleRegistry
from dfe_engine.hunts.validator import HuntValidator

REFUSED = [
    "url('http://203.0.113.9/x', 'JSONEachRow')",
    "dfe.main extra",
    "dfe.main()",
    "dfe.main) -- x",
    "dfe.ma'in",
    "dfe.main -- trailing",
    "cluster.dfe.main",
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


def _save_rule(rules_dir: Path, **source) -> None:
    registry = RuleRegistry(rules_directory=rules_dir, writable=True, refresh_interval=0)
    try:
        registry.save(Rule(rule_id="certutil", name="Certutil", where_clause="a = 1", **source))
    finally:
        registry.close()


@pytest.mark.parametrize("source", ["dfe.main", "main", "  _tenant_a.main_2  "])
def test_a_plain_hunt_source_validates(tmp_path: Path, source):
    _validate(tmp_path, _hunt(global_source_table_name=source))


@pytest.mark.parametrize("source", REFUSED)
def test_a_hunt_source_that_is_not_a_table_name_is_refused(tmp_path: Path, source):
    with pytest.raises(ValueError) as caught:
        _validate(tmp_path, _hunt(global_source_table_name=source))

    assert str(caught.value).startswith("Invalid 'global_source_table_name': ")
    assert "is not a table name" in str(caught.value)


@pytest.mark.parametrize("source", REFUSED)
def test_a_rule_entry_source_that_is_not_a_table_name_is_refused(tmp_path: Path, source):
    rules = [{"rule_name": "certutil", "source_table_name": source}]

    with pytest.raises(ValueError) as caught:
        _validate(tmp_path, _hunt(rules=rules))

    assert str(caught.value).startswith("Invalid 'source_table_name' of rule 'certutil': ")


@pytest.mark.parametrize(
    "source",
    [
        {"source_db": "dfe", "source_table": "main) -- x"},
        {"source_db": "dfe", "source_table": "ma in"},
        {"source_db": "d'fe", "source_table": "main"},
        {"source_db": "a.b", "source_table": "main"},
    ],
)
def test_a_rule_file_whose_source_is_not_a_table_name_is_refused(tmp_path: Path, source):
    _save_rule(tmp_path, **source)

    with pytest.raises(ValueError) as caught:
        _validate(tmp_path, _hunt())

    assert str(caught.value).startswith("Invalid source of rule file certutil.yaml: ")
    assert "is not a table name" in str(caught.value)


def test_a_rule_file_with_a_plain_source_validates(tmp_path: Path):
    _save_rule(tmp_path, source_db="acme", source_table="windows_audit")

    _validate(tmp_path, _hunt())


def test_a_rule_file_with_no_source_validates(tmp_path: Path):
    _save_rule(tmp_path)

    _validate(tmp_path, _hunt())


def test_a_hunt_source_that_is_not_text_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match="It should be a table name"):
        _validate(
            tmp_path,
            _hunt(
                global_source_table_name=["dfe", "main"],
                rules=[{"rule_name": "certutil", "source": "windows-audit"}],
            ),
        )


def test_a_blank_hunt_source_still_defers_to_the_rules_own_source(tmp_path: Path):
    rules = [{"rule_name": "certutil", "source": "windows-audit"}]

    _validate(tmp_path, _hunt(global_source_table_name="", rules=rules))
