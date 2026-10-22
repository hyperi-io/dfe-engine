#  Project:      dfe-engine
#  File:         tests/integration/test_docustore_org_races.py
#  Purpose:      Two engines on one document store keep orgs and ingest credentials consistent
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Two whole engines racing org and ingest credential writes through the API.

Each engine has its own config directory, as each replica has its own pod volume,
and both use one document store database (``mongo_uri``). The shared secrets
directory stands in for the shared secrets backend a deployment with more than one
engine requires.
"""

import functools
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, create_access_token
from dfe_engine.orgs.ingest_tokens import bearer_entries, bearer_secret_path
from dfe_engine.settings import (
    AccountStoreSettings,
    APISettings,
    AuthSettings,
    ClickHouseSettings,
    DFESettings,
    HuntsSettings,
    LocalAuthSettings,
    SecretsSettings,
)
from dfe_engine.store.documents import DocuStore
from dfe_engine.yaml_utils import yaml_load
from tests.support.accounts import admin_on_its_own_password
from tests.support.races import race, raised

pytestmark = pytest.mark.integration

ADMIN_PASSWORD = "test-admin-pw"
CREATE = {"If-None-Match": "*"}
ROUNDS = 10
CREDENTIALS = "/api/v1/orgs/acme/ingest-credentials"


def _settings(root: Path, shared: Path, mongo_uri: str, database: str) -> DFESettings:
    """One engine's settings: its own config directory, the shared store, secrets and hunts."""
    (root / "auth").mkdir(parents=True)
    return DFESettings(
        config_dir=str(root),
        clickhouse=ClickHouseSettings(bootstrap_tables=False),
        hunts=HuntsSettings(rules_dir=str(shared / "rules"), hunt_dir=str(shared / "hunts")),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(root / "auth"),
            store_backend="document",
            accounts_store=AccountStoreSettings(uri=mongo_uri, database=database),
            local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
        ),
        secrets=SecretsSettings(provider="file", path=str(shared / "secrets")),
        api=APISettings(jwt_secret="test-secret-key-for-unit-tests-hmac32"),
    )


@pytest.fixture
def engines(mongo_uri: str, tmp_path: Path) -> Iterator[SimpleNamespace]:
    """Engines ``a`` and ``b`` on one throwaway database, with an admin's bearer header."""
    database = f"dfe_engine_test_{uuid.uuid4().hex[:8]}"
    shared = tmp_path / "shared"
    for sub in ("secrets", "rules", "hunts"):
        (shared / sub).mkdir(parents=True)
    first = _settings(tmp_path / "a", shared, mongo_uri, database)
    second = _settings(tmp_path / "b", shared, mongo_uri, database)
    app_a, app_b = create_app(settings=first), create_app(settings=second)
    token = create_access_token(data={"sub": "admin"}, settings=first)
    try:
        with (
            TestClient(app_a, raise_server_exceptions=False) as a,
            TestClient(app_b, raise_server_exceptions=False) as b,
        ):
            admin_on_its_own_password(app_a)
            yield SimpleNamespace(
                a=a,
                b=b,
                apps=(app_a, app_b),
                headers={"Authorization": f"Bearer {token}"},
                hunt_dir=shared / "hunts",
            )
    finally:
        _registries.clear()
        store = DocuStore(mongo_uri, database)
        store.drop_database(database)
        store.close()


def _codes(results: list[Response | BaseException]) -> list[int]:
    """Each response's status, in call order. A call that raised fails the test."""
    assert raised(results) == []
    return [r.status_code for r in results if isinstance(r, Response)]


def _holders(client: TestClient, headers: dict[str, str], tenant_id: str) -> list[str]:
    orgs = client.get("/api/v1/orgs?per_page=-1", headers=headers).json()["items"]
    return [org["name"] for org in orgs if tenant_id in org["org_ids"]]


def test_two_orgs_sharing_a_tenant_id_through_two_engines_one_is_refused(engines):
    for round_ in range(ROUNDS):
        shared = f"shared-{round_}"
        results = race(
            [
                functools.partial(
                    engines.a.post,
                    "/api/v1/orgs",
                    json={"name": f"alpha-{round_}", "org_ids": [shared]},
                    headers=engines.headers,
                ),
                functools.partial(
                    engines.b.post,
                    "/api/v1/orgs",
                    json={"name": f"beta-{round_}", "org_ids": [shared]},
                    headers=engines.headers,
                ),
            ]
        )

        codes = sorted(_codes(results))
        assert codes == [201, 409], f"round {round_}: {codes}"
        assert len(_holders(engines.a, engines.headers, shared)) == 1
        assert _holders(engines.a, engines.headers, shared) == _holders(
            engines.b, engines.headers, shared
        )


def _bearer_secret_is_the_accepted_set(app) -> None:
    """Each listener's bearer secret holds exactly the credentials this engine's store lists."""
    registry, secrets = app.state.org_registry, app.state.dfe_secrets
    for listener, content in bearer_entries(registry.ingest_credentials.list(), secrets).items():
        assert secrets.get_or_none(bearer_secret_path(listener)) == content, listener


def test_a_credential_made_on_one_engine_while_another_goes_on_the_other(engines):
    made = engines.a.post("/api/v1/orgs", json={"name": "acme"}, headers=engines.headers)
    assert made.status_code == 201, made.text
    for round_ in range(ROUNDS):
        doomed = engines.b.put(
            f"{CREDENTIALS}/old-{round_}", json={}, headers={**engines.headers, **CREATE}
        )
        assert doomed.status_code == 201, doomed.text

        results = race(
            [
                functools.partial(
                    engines.a.put,
                    f"{CREDENTIALS}/new-{round_}",
                    json={},
                    headers={**engines.headers, **CREATE},
                ),
                functools.partial(
                    engines.b.delete,
                    f"{CREDENTIALS}/old-{round_}",
                    headers={**engines.headers, "If-Match": doomed.headers["ETag"]},
                ),
            ]
        )

        assert _codes(results) == [201, 204], f"round {round_}"
        for app in engines.apps:
            _bearer_secret_is_the_accepted_set(app)
        http = engines.apps[0].state.dfe_secrets.get_or_none(bearer_secret_path("http")) or ""
        assert sorted(line.split(":", 1)[0] for line in http.split()) == [
            f"new-{n}" for n in range(round_ + 1)
        ]


ORGS = "/api/v1/orgs"
_HUNT = {
    "cron": "*/5 * * * *",
    "rules": ["certutil"],
    "global_source_table_name": "dfe.main",
    "global_target_table_name": "dfe.detection",
}


def _org(engines, client: TestClient, name: str, org_ids: list[str] | None = None) -> None:
    body = {"name": name, "org_ids": org_ids or []}
    made = client.post(ORGS, json=body, headers=engines.headers)
    assert made.status_code == 201, made.text


def _hunt_naming(engines, name: str, customers: list[str]) -> None:
    body = {"name": name, **_HUNT, "customers": customers}
    made = engines.a.post("/api/v1/hunts", json=body, headers=engines.headers)
    assert made.status_code == 201, made.text


def _hunt_tenant_ids(engines, name: str) -> dict[str, list[str]]:
    return yaml_load(engines.hunt_dir / f"{name}.yaml").get("customer_tenant_ids") or {}


def _the_hunt_write_waits_for(monkeypatch, write) -> None:
    """Make *write* land the moment a route writes an org's tenant ids into its hunts."""
    hunts = _registries["hunt_configs"]
    real = hunts.store_org_tenant_ids

    def racing(*args, **kwargs):
        monkeypatch.setattr(hunts, "store_org_tenant_ids", real)
        write()
        return real(*args, **kwargs)

    monkeypatch.setattr(hunts, "store_org_tenant_ids", racing)


def test_an_org_that_loses_its_tenant_id_to_another_engine_leaves_its_hunts_alone(
    engines, monkeypatch
):
    _org(engines, engines.a, "beta")
    _hunt_naming(engines, "beta_hunt", ["beta"])
    assert engines.a.delete(f"{ORGS}/beta", headers=engines.headers).status_code == 204
    other = {}

    def b_takes_the_tenant_id() -> None:
        body = {"name": "alpha", "org_ids": ["shared"]}
        other["resp"] = engines.b.post(ORGS, json=body, headers=engines.headers)

    _the_hunt_write_waits_for(monkeypatch, b_takes_the_tenant_id)

    created = engines.a.post(
        ORGS, json={"name": "beta", "org_ids": ["shared"]}, headers=engines.headers
    )

    assert sorted([created.status_code, other["resp"].status_code]) == [201, 409]
    (holder,) = _holders(engines.a, engines.headers, "shared")
    holding = [
        org for org, ids in _hunt_tenant_ids(engines, "beta_hunt").items() if "shared" in ids
    ]
    assert holding in ([], [holder])


def test_a_credential_binding_an_org_another_engine_is_deleting_loses_cleanly(engines, monkeypatch):
    _org(engines, engines.a, "acme")
    _org(engines, engines.a, "beta", ["beta-1"])
    _hunt_naming(engines, "beta_hunt", ["beta"])
    bound = {}

    def a_binds_beta() -> None:
        bound["resp"] = engines.a.put(
            f"{CREDENTIALS}/agents",
            json={"orgs": ["acme", "beta"]},
            headers={**engines.headers, **CREATE},
        )

    _the_hunt_write_waits_for(monkeypatch, a_binds_beta)

    deleted = engines.b.delete(f"{ORGS}/beta", headers=engines.headers)

    assert sorted([deleted.status_code, bound["resp"].status_code]) == [204, 409], (
        deleted.text,
        bound["resp"].text,
    )
    orgs = {org.name for org in engines.apps[0].state.org_registry.list()}
    credentials = engines.apps[0].state.org_registry.ingest_credentials.list()
    assert all(set(c.orgs) <= orgs for c in credentials), credentials
    beta_ids = _hunt_tenant_ids(engines, "beta_hunt").get("beta")
    assert beta_ids == (["beta-1"] if "beta" in orgs else None)
