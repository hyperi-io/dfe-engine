#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_hunt_customer_orgs.py
#  Purpose:      A hunt's customers are org names, saved with the tenant ids they resolve to
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""POST and PUT /api/v1/hunts resolve each customer org to its tenant ids, as the runner reads them.

The console sends org names, and the runner quoted them as ``_org_id`` values, so
an org declaring tenant ids got a hunt that matched nothing. The runner cannot
read the org registry, so the engine stores what each name resolves to.
"""

import pytest
from fastapi.testclient import TestClient

from dfe_engine.hunt_runner import cli
from dfe_engine.hunt_runner.spec_loader import load_specs
from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleRegistry
from dfe_engine.yaml_utils import yaml_load

HUNTS = "/api/v1/hunts"
_HUNT = {
    "name": "windows_hunt",
    "cron": "*/5 * * * *",
    "rules": ["certutil"],
    "global_source_table_name": "dfe.main",
    "global_target_table_name": "dfe.detection",
}


@pytest.fixture
def orgs(client: TestClient, admin_headers: dict[str, str]) -> None:
    for body in (
        {"name": "acme", "org_ids": ["acme-1", "acme-2"]},
        {"name": "globex"},
        {"name": "initech", "org_ids": ["initech-1"]},
    ):
        resp = client.post("/api/v1/orgs", json=body, headers=admin_headers)
        assert resp.status_code == 201, resp.text


def _stored(api_settings) -> dict:
    return yaml_load(f"{api_settings.hunts.hunt_dir}/windows_hunt.yaml")


def test_each_customer_is_saved_with_the_tenant_ids_it_resolves_to(
    client, admin_headers, api_settings, orgs
):
    resp = client.post(
        HUNTS, json={**_HUNT, "customers": ["acme", "globex"]}, headers=admin_headers
    )

    assert resp.status_code == 201, resp.text
    resolved = {"acme": ["acme-1", "acme-2"], "globex": ["globex"]}
    assert resp.json()["customer_tenant_ids"] == resolved
    assert _stored(api_settings)["customers"] == ["acme", "globex"]
    assert _stored(api_settings)["customer_tenant_ids"] == resolved


def test_the_runner_reads_the_resolved_tenant_ids_not_the_org_names(
    client, admin_headers, api_settings, orgs
):
    rules = RuleRegistry(
        rules_directory=api_settings.hunts.rules_dir, writable=True, refresh_interval=0
    )
    try:
        rules.save(
            Rule(rule_id="certutil", name="Certutil", where_clause="process_name = 'certutil.exe'")
        )
    finally:
        rules.close()
    assert (
        client.post(HUNTS, json={**_HUNT, "customers": ["acme"]}, headers=admin_headers).status_code
        == 201
    )
    database = api_settings.clickhouse.effective_data_database

    specs = load_specs(api_settings.hunts.hunt_dir, **cli._spec_sources(api_settings, database))

    (statement,) = specs["windows_hunt"].queries
    assert "_org_id IN ('acme-1', 'acme-2')" in statement.sql
    assert "'acme'" not in statement.sql


def test_a_customer_naming_no_registered_org_is_refused_and_nothing_is_saved(
    client, admin_headers, api_settings, orgs
):
    resp = client.post(HUNTS, json={**_HUNT, "customers": ["acme", "ghost"]}, headers=admin_headers)

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "unknown_org"
    assert "'ghost'" in resp.json()["message"]
    assert client.get(f"{HUNTS}/windows_hunt", headers=admin_headers).status_code == 404


def test_a_tenant_id_is_not_accepted_in_place_of_an_org_name(client, admin_headers, orgs):
    resp = client.post(HUNTS, json={**_HUNT, "customers": ["acme-1"]}, headers=admin_headers)

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "unknown_org"


def test_an_update_resolves_the_customers_afresh(client, admin_headers, api_settings, orgs):
    assert (
        client.post(HUNTS, json={**_HUNT, "customers": ["acme"]}, headers=admin_headers).status_code
        == 201
    )
    body = {key: value for key, value in _HUNT.items() if key != "name"}

    resp = client.put(
        f"{HUNTS}/windows_hunt", json={**body, "customers": ["initech"]}, headers=admin_headers
    )

    assert resp.status_code == 200, resp.text
    assert _stored(api_settings)["customer_tenant_ids"] == {"initech": ["initech-1"]}


def test_tenant_ids_a_caller_sends_are_never_stored(client, admin_headers, api_settings, orgs):
    resp = client.post(
        HUNTS,
        json={**_HUNT, "customers": ["globex"], "customer_tenant_ids": {"globex": ["acme-1"]}},
        headers=admin_headers,
    )

    assert resp.status_code == 201, resp.text
    assert _stored(api_settings)["customer_tenant_ids"] == {"globex": ["globex"]}
