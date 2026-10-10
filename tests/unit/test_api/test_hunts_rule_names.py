"""A hunt write refuses a rule name that could leave the rules directory.

A hunt may also reference only a name a rule can be created under.
"""

from pathlib import Path

import pytest

from dfe_engine.api.v1.rules import RuleCreateRequest
from dfe_engine.hunts.rule_names import validate_rule_name

_HUNT = {
    "display_name": "Certutil Abuse",
    "cron": "*/5 * * * *",
    "customers": ["org_a"],
    "global_source_table_name": "dfe.main",
    "global_target_table_name": "dfe.detection",
}

_ESCAPES = ["../x", "a/b", "/etc/passwd", "", "..", "a\x00b"]


@pytest.mark.parametrize("rule", _ESCAPES)
def test_create_refuses_the_rule_name_and_saves_nothing(client, admin_headers, rule):
    resp = client.post(
        "/api/v1/hunts",
        json={"name": "windows_hunt", **_HUNT, "rules": [rule]},
        headers=admin_headers,
    )

    assert resp.status_code == 422, resp.text
    assert client.get("/api/v1/hunts/windows_hunt", headers=admin_headers).status_code == 404


@pytest.mark.parametrize("rule", _ESCAPES)
def test_update_refuses_the_rule_name_and_keeps_the_stored_rules(client, admin_headers, rule):
    created = client.post(
        "/api/v1/hunts",
        json={"name": "windows_hunt", **_HUNT, "rules": ["certutil"]},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text

    resp = client.put(
        "/api/v1/hunts/windows_hunt",
        json={**_HUNT, "rules": ["certutil", rule]},
        headers=admin_headers,
    )

    assert resp.status_code == 422, resp.text
    stored = client.get("/api/v1/hunts/windows_hunt", headers=admin_headers).json()
    assert [entry["rule_name"] for entry in stored["rules"]] == ["certutil"]


def test_a_dotted_rule_name_is_refused_because_no_rule_can_be_created_under_it(
    client, admin_headers
):
    resp = client.post(
        "/api/v1/hunts",
        json={"name": "windows_hunt", **_HUNT, "rules": ["win.cert"]},
        headers=admin_headers,
    )

    assert resp.status_code == 422, resp.text
    assert client.get("/api/v1/hunts/windows_hunt", headers=admin_headers).status_code == 404


def _accepted(check) -> bool:
    try:
        check()
    except ValueError:
        return False
    return True


@pytest.mark.parametrize(
    "name",
    [
        "certutil",
        "win_cert-01",
        "sigma_windows-audit_0f1e2d3c",
        "win.cert",
        ".hidden",
        "trailing.",
        "-",
        "_",
        "has space",
        "line\n",
        "caf\u00e9",
        "",
        "..",
        "a/b",
    ],
)
def test_a_hunt_reference_and_rule_create_accept_the_same_names(name):
    creatable = _accepted(
        lambda: RuleCreateRequest(name=name, user_sql="SELECT 1 FROM dfe.main WHERE a = 1")
    )
    referencable = _accepted(lambda: validate_rule_name(name))

    assert referencable == creatable


_RULE = {
    "display_name": "Certutil Abuse",
    "severity": "high",
    "user_sql": "SELECT * FROM dfe.main WHERE severity = 'high'",
}

_LIMIT = 128


def test_a_rule_name_at_the_limit_is_created_and_referenced(client, admin_headers, api_settings):
    name = "r" * _LIMIT

    created = client.post("/api/v1/rules", json={"name": name, **_RULE}, headers=admin_headers)
    hunt = client.post(
        "/api/v1/hunts",
        json={"name": "windows_hunt", **_HUNT, "rules": [name]},
        headers=admin_headers,
    )

    assert created.status_code == 201, created.text
    assert (Path(api_settings.hunts.rules_dir) / f"{name}.yaml").is_file()
    assert hunt.status_code == 201, hunt.text


@pytest.mark.parametrize("length", [_LIMIT + 1, 300])
def test_a_rule_name_over_the_limit_is_refused_by_create_and_by_a_hunt(
    client, admin_headers, api_settings, length
):
    name = "r" * length

    created = client.post("/api/v1/rules", json={"name": name, **_RULE}, headers=admin_headers)
    hunt = client.post(
        "/api/v1/hunts",
        json={"name": "windows_hunt", **_HUNT, "rules": [name]},
        headers=admin_headers,
    )

    assert created.status_code == 422, created.text
    assert not (Path(api_settings.hunts.rules_dir) / f"{name}.yaml").exists()
    assert hunt.status_code == 422, hunt.text
    assert client.get("/api/v1/hunts/windows_hunt", headers=admin_headers).status_code == 404
