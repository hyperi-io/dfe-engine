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

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from dfe_engine.api.v1.hunts import HuntCreateRequest
from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleRegistry
from dfe_engine.yaml_utils import yaml_dump_string

# The hunt YAML key the engine writes and the runner reads, pinned as the file contract.
CUSTOMER_TENANT_IDS = "customer_tenant_ids"


def resolved_customers(*names: str) -> dict[str, Any]:
    """``customers`` and the tenant ids the API stores for them, each org declaring none.

    An org with no ``org_ids`` resolves to its own name, so each name maps to itself.
    """
    return {"customers": list(names), CUSTOMER_TENANT_IDS: {name: [name] for name in names}}


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


def write_hunt(
    hunts_dir: Path,
    hunt: str,
    rule_id: str,
    db: str,
    *,
    source: str = "main",
    customers: Sequence[str] = ("acme",),
    tenant_ids: Mapping[str, list[str]] | None = None,
) -> None:
    """Write one hunt file exactly as POST /api/v1/hunts writes it, firing every minute.

    *customers* are org names. *tenant_ids* is what the API resolved each to, and
    defaults to each org declaring none, so a name reads the rows carrying it.
    """
    body = HuntCreateRequest(
        name=hunt,
        cron="* * * * *",
        rules=[rule_id],
        customers=list(customers),
        global_source_table_name=f"{db}.{source}",
        global_target_table_name=f"{db}.detection",
    )
    config = body.to_config_dict(hunt_name=hunt)
    resolved = resolved_customers(*customers)[CUSTOMER_TENANT_IDS]
    config[CUSTOMER_TENANT_IDS] = dict(tenant_ids) if tenant_ids is not None else resolved
    hunts_dir.mkdir(parents=True, exist_ok=True)
    (hunts_dir / f"{hunt}.yaml").write_text(
        yaml_dump_string(config),
        encoding="utf-8",
        newline="\n",
    )
