"""A hunt write refuses a rule name that could leave the rules directory."""

import pytest

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


def test_a_dotted_rule_name_is_accepted(client, admin_headers):
    resp = client.post(
        "/api/v1/hunts",
        json={"name": "windows_hunt", **_HUNT, "rules": ["win.cert"]},
        headers=admin_headers,
    )

    assert resp.status_code == 201, resp.text
    assert [entry["rule_name"] for entry in resp.json()["rules"]] == ["win.cert"]
