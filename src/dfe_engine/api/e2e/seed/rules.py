#  Project:      dfe-engine
#  File:         api/e2e/seed/rules.py
#  Purpose:      Rule seeder primitives for e2e-server
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Rule seeding. Public methods are the scripts. Privates are reusable.

A seeded rule is a stored detection the list can search. The WHERE matches
nothing, so leaving one of these enabled does not alert on every row.
"""

from __future__ import annotations

from dfe_engine.api.e2e.seed.base import Seed
from dfe_engine.api.e2e.seed.sources import SEED_ACTOR
from dfe_engine.hunts.rule_model import Rule

_WHERE = "1 = 0"


def searchable_rule_names() -> list[str]:
    """Rule file stems ``seed_searchable_rules`` writes."""
    return [f"searchable-rule-{num}" for num in range(50)]


def _rule(name: str, num: int) -> Rule:
    """One stored rule, named for the hunt of the same index."""
    return Rule(
        rule_id=name,
        name=f"Searchable Rule {num}",
        severity="medium",
        where_clause=_WHERE,
        original_sql=f"SELECT 1 WHERE {_WHERE}",
        hunt_name=f"searchable-hunt-{num}",
    )


class Rules(Seed):
    """Create or reset detection rules in the rule registry."""

    def seed_searchable_rules(self) -> bool:
        """Seed fifty rules a list search can filter.

        Returns True when any rule was written, False when they were all already there.
        """
        registry = self._require_rule_registry()
        changed = False
        for num, name in enumerate(searchable_rule_names()):
            if registry.name_exists(name):
                continue
            registry.save(_rule(name, num), created_by=SEED_ACTOR)
            changed = True
        return changed

    def delete_all(self) -> None:
        """Clear every detection rule. A no-op when no rule registry is configured."""
        registry = self._rule_registry
        if registry is None:
            return
        for row in registry.list_rules():
            registry.delete(str(row["name"]), created_by=SEED_ACTOR)
