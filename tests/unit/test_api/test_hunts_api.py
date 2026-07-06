#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_hunts_api.py
#  Purpose:      Hunts API round-trip - per-rule YAML overrides survive PUT
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""PUT /hunts/{name} must not discard per-rule YAML overrides.

The write API accepts rule NAMES only; per-rule overrides (target_table_name,
source, initial_checkpoint_lookback_minutes) are set by editing hunt YAML -
the documented YAML-edit path. An API update of an UNRELATED field must leave
those overrides in place on disk for every rule that remains listed.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from dfe_engine.api.deps import _registries


def _stored_hunt() -> dict:
    return {
        "display_name": "Edge Auth",
        "cron": "*/5 * * * *",
        "log_buffer": 60,
        "global_target_table_name": "hunt_results",
        "customers": ["acme"],
        "rules": [
            {
                "rule_name": "r1",
                "target_table_name": "custom_results",
                "source": "edge",
                "initial_checkpoint_lookback_minutes": 90,
            },
            {"rule_name": "r2"},
        ],
    }


def _put_body(**overrides) -> dict:
    body = {
        "display_name": "Edge Auth",
        "cron": "*/5 * * * *",
        "log_buffer": 60,
        "global_target_table_name": "hunt_results",
        "customers": ["acme"],
        "rules": ["r1", "r2"],
    }
    body.update(overrides)
    return body


def _hunt_yaml(api_settings, name: str) -> dict:
    hunt_dir = Path(api_settings.hunts.hunt_dir.split(",")[0].strip())
    return yaml.safe_load((hunt_dir / f"{name}.yaml").read_text())


def test_put_preserves_per_rule_yaml_overrides(client, admin_headers, api_settings):
    _registries["hunt_configs"].save("edge_auth", _stored_hunt())

    resp = client.put(
        "/api/v1/hunts/edge_auth",
        json=_put_body(log_buffer=120),  # unrelated field change
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text

    on_disk = _hunt_yaml(api_settings, "edge_auth")
    assert on_disk["log_buffer"] == 120  # the requested change landed
    assert on_disk["rules"][0] == {
        "rule_name": "r1",
        "target_table_name": "custom_results",
        "source": "edge",
        "initial_checkpoint_lookback_minutes": 90,
    }
    assert on_disk["rules"][1] == {"rule_name": "r2"}

    # the response reflects the preserved overrides too
    rules = resp.json()["rules"]
    assert rules[0]["target_table_name"] == "custom_results"
    assert rules[0]["initial_checkpoint_lookback_minutes"] == 90


def test_put_preserves_api_written_alerts_block(client, admin_headers, api_settings):
    # P2.5: the alerts.destinations block (written by POST /alerts/destinations)
    # is not part of HuntWriteRequest, so a PUT that rebuilds the YAML from the
    # request model must preserve it rather than silently drop it.
    stored = _stored_hunt()
    stored["alerts"] = {"destinations": ["slack-alerts"]}
    _registries["hunt_configs"].save("edge_auth3", stored)

    resp = client.put(
        "/api/v1/hunts/edge_auth3",
        json=_put_body(log_buffer=90),
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text

    on_disk = _hunt_yaml(api_settings, "edge_auth3")
    assert on_disk["log_buffer"] == 90  # the requested change landed
    assert on_disk["alerts"] == {"destinations": ["slack-alerts"]}  # block survived


def test_put_drops_overrides_only_for_removed_rules(client, admin_headers, api_settings):
    _registries["hunt_configs"].save("edge_auth2", _stored_hunt())

    resp = client.put(
        "/api/v1/hunts/edge_auth2",
        json=_put_body(rules=["r2", "r3"]),  # r1 removed, r3 added
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text

    on_disk = _hunt_yaml(api_settings, "edge_auth2")
    assert on_disk["rules"] == [{"rule_name": "r2"}, {"rule_name": "r3"}]
