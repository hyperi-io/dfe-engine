#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_list_hunts_rule_forms.py
#  Purpose:      list_hunts reads legacy string-form rule entries (P3.9)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""list_hunts must report rule names from BOTH dict-form and legacy string-form
rule entries, matching the API reader. A dict-only reader made the two disagree,
so hunt_names_referencing_rule missed string-rule hunts and DELETE /rules could
drop a still-in-use rule (P3.9)."""

from __future__ import annotations

from dfe_engine.hunts.hunt_config_registry import HuntConfigRegistry


def test_list_hunts_includes_string_form_rules(tmp_path):
    reg = HuntConfigRegistry(tmp_path / "hunts", writable=True, refresh_interval=0)
    # a hand-authored/legacy hunt whose rules are plain strings, plus one dict form
    reg.save(
        "legacy",
        {
            "display_name": "Legacy",
            "cron": "*/5 * * * *",
            "customers": ["acme"],
            "rules": ["r1", {"rule_name": "r2"}],
        },
    )
    listed = {h["name"]: h for h in reg.list_hunts()}
    assert set(listed["legacy"]["rules"]) == {"r1", "r2"}  # string r1 NOT dropped
