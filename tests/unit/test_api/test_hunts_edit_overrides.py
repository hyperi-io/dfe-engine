#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_hunts_edit_overrides.py
#  Purpose:      An API edit of a hunt keeps the per-rule overrides its stored YAML holds
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A hunt written straight to the deploy repo can give a rule its own source and target.

The API takes rules by name only, so an edit through it keeps the stored entry of each
rule that stays in the hunt. A rule the edit drops goes with its entry, and a rule it
adds is stored by name.
"""

from pathlib import Path

import pytest
from common.hunt_files import store_rule_file

from dfe_engine.api.deps import get_hunt_config_registry

HUNTS = "/api/v1/hunts"
_HUNT = {
    "cron": "*/5 * * * *",
    "customers": ["org_a"],
    "global_source_table_name": "dfe.main",
    "global_target_table_name": "dfe.detection",
}
_KEPT = {
    "rule_name": "certutil",
    "source_table_name": "dfe.winaudit",
    "target_table_name": "dfe.windows_detections",
}
_DROPPED = {"rule_name": "netcat", "source_table_name": "dfe.linux"}


@pytest.fixture
def hunt_with_overrides(client, admin_headers, api_settings, customer_orgs) -> None:
    """A hunt whose stored YAML gives two rules their own source and target."""
    rules_dir = Path(api_settings.hunts.rules_dir)
    for name in ("certutil", "netcat", "added"):
        store_rule_file(rules_dir, name)
    created = client.post(
        HUNTS,
        json={"name": "windows_hunt", **_HUNT, "rules": ["certutil", "netcat"]},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    registry = get_hunt_config_registry()
    stored = registry.get("windows_hunt")
    registry.save("windows_hunt", {**stored, "rules": [dict(_KEPT), dict(_DROPPED)]})


@pytest.mark.usefixtures("hunt_with_overrides")
@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        pytest.param(
            ["certutil", "netcat"], [_KEPT, _DROPPED], id="every rule stays, every entry stays"
        ),
        pytest.param(["certutil"], [_KEPT], id="a dropped rule goes with its entry"),
        pytest.param(
            ["certutil", "added"],
            [_KEPT, {"rule_name": "added"}],
            id="an added rule is stored by name",
        ),
    ],
)
def test_an_edit_keeps_the_entry_of_each_rule_that_stays(client, admin_headers, rules, expected):
    resp = client.put(
        f"{HUNTS}/windows_hunt",
        json={**_HUNT, "cron": "*/10 * * * *", "rules": rules},
        headers=admin_headers,
    )

    assert resp.status_code == 200, resp.text
    stored = get_hunt_config_registry().get("windows_hunt")
    assert stored["cron"] == "*/10 * * * *"
    assert stored["rules"] == expected
