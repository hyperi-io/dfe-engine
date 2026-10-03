#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_hunts_rule_file.py
#  Purpose:      A hunt write checks the rule file the hunt runner reads
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A hunt write checks each named rule as the ``{name}.yaml`` the runner reads.

The rule registry writes ``{name}.yaml`` and the runner's rule compiler reads the
same file, so that is the file a hunt write looks for: in ``hunts.rules_dir``, or
in the deploy repo's ``config/rules`` once the registry is backed by it. A rule
not there yet is a warning, because a hunt may be drafted before its rules; a file
the runner could not read as a YAML mapping is refused.
"""

from pathlib import Path

import pytest

from dfe_engine.api.deps import _registries
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.hunts.deploy_repo import DeployRepoStore
from dfe_engine.hunts.rule_registry import RuleRegistry

_HUNT = {
    "display_name": "Certutil Abuse",
    "cron": "*/5 * * * *",
    "customers": ["org_a"],
    "global_source_table_name": "dfe.main",
    "global_target_table_name": "dfe.detection",
}

_RULE = {
    "name": "certutil",
    "display_name": "Certutil Abuse",
    "severity": "high",
    "user_sql": "SELECT * FROM dfe.main WHERE severity = 'high'",
}

MISSING = "Rule file not found"


def _create_hunt(client, headers, rule: str):
    return client.post(
        "/api/v1/hunts", json={"name": "windows_hunt", **_HUNT, "rules": [rule]}, headers=headers
    )


def _missing_warnings(events: list[dict]) -> list[str]:
    return [event["event"] for event in events if MISSING in event["event"]]


def test_a_hunt_naming_a_stored_rule_finds_its_yaml(
    client, admin_headers, api_settings, audit_events
):
    assert client.post("/api/v1/rules", json=_RULE, headers=admin_headers).status_code == 201
    assert (Path(api_settings.hunts.rules_dir) / "certutil.yaml").is_file()

    resp = _create_hunt(client, admin_headers, "certutil")

    assert resp.status_code == 201, resp.text
    assert _missing_warnings(audit_events) == []


def test_a_hunt_naming_a_missing_rule_warns_on_its_yaml_and_is_saved(
    client, admin_headers, api_settings, audit_events
):
    resp = _create_hunt(client, admin_headers, "absent")

    assert resp.status_code == 201, resp.text
    expected = Path(api_settings.hunts.rules_dir) / "absent.yaml"
    [warning] = _missing_warnings(audit_events)
    assert str(expected) in warning


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("- a list\n- not a mapping\n", id="not-a-mapping"),
        pytest.param("where_clause: [unclosed\n", id="not-yaml"),
    ],
)
def test_a_rule_file_the_runner_cannot_read_is_refused(
    client, admin_headers, api_settings, content
):
    (Path(api_settings.hunts.rules_dir) / "broken.yaml").write_text(content, encoding="utf-8")

    resp = _create_hunt(client, admin_headers, "broken")

    assert resp.status_code == 422, resp.text
    assert "broken.yaml" in resp.json()["message"]


def test_under_gitops_the_hunt_checks_the_deploy_repos_rules(
    client, admin_headers, tmp_path, audit_events
):
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    crud = GitCrud(repo, default_registry())
    store = DeployRepoStore(crud, "rules", environment="dev", mode="team")
    _registries.pop("rules").close()
    _registries["rules"] = RuleRegistry(deploy_repo=store)
    assert client.post("/api/v1/rules", json=_RULE, headers=admin_headers).status_code == 201
    assert (crud.repo_path / "config" / "rules" / "certutil.yaml").is_file()

    resp = _create_hunt(client, admin_headers, "certutil")

    assert resp.status_code == 201, resp.text
    assert _missing_warnings(audit_events) == []
