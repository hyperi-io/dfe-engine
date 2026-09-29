#  Project:      dfe-engine
#  File:         tests/common/hunt_files.py
#  Purpose:      Write rule and hunt YAML the way the API writes them
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rule and hunt files built from the API's own models, for the runner to load.

Nothing here is shaped to suit the runner: the rule goes through ``RuleRegistry``
and the hunt through ``HuntCreateRequest.to_config_dict``, which are what
``POST /api/v1/rules`` and ``POST /api/v1/hunts`` write with.
"""

from pathlib import Path

from dfe_engine.api.v1.hunts import HuntCreateRequest
from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleRegistry
from dfe_engine.yaml_utils import yaml_dump_string


def write_rule(
    rules_dir: Path,
    rule_id: str,
    where_clause: str,
    *,
    name: str = "Marked Org Activity",
    severity: str = "high",
) -> None:
    """Write one rule file exactly as POST /api/v1/rules writes it."""
    registry = RuleRegistry(rules_directory=rules_dir, writable=True, refresh_interval=0)
    try:
        registry.save(
            Rule(rule_id=rule_id, name=name, severity=severity, where_clause=where_clause)
        )
    finally:
        registry.close()


def write_hunt(hunts_dir: Path, hunt: str, rule_id: str, db: str) -> None:
    """Write one hunt file exactly as POST /api/v1/hunts writes it, firing every minute."""
    body = HuntCreateRequest(
        name=hunt,
        cron="* * * * *",
        rules=[rule_id],
        customers=["acme"],
        global_source_table_name=f"{db}.main",
        global_target_table_name=f"{db}.detection",
    )
    hunts_dir.mkdir(parents=True, exist_ok=True)
    (hunts_dir / f"{hunt}.yaml").write_text(
        yaml_dump_string(body.to_config_dict(hunt_name=hunt)),
        encoding="utf-8",
        newline="\n",
    )
