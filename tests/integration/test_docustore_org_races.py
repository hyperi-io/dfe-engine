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
    LocalAuthSettings,
    SecretsSettings,
)
from dfe_engine.store.documents import DocuStore
from tests.support.accounts import admin_on_its_own_password
from tests.support.races import race, raised

pytestmark = pytest.mark.integration

ADMIN_PASSWORD = "test-admin-pw"
CREATE = {"If-None-Match": "*"}
ROUNDS = 10
CREDENTIALS = "/api/v1/orgs/acme/ingest-credentials"


def _settings(root: Path, secrets_dir: Path, mongo_uri: str, database: str) -> DFESettings:
    """One engine's settings: its own config directory, the shared store and secrets."""
    (root / "auth").mkdir(parents=True)
    return DFESettings(
        config_dir=str(root),
        clickhouse=ClickHouseSettings(bootstrap_tables=False),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(root / "auth"),
            store_backend="document",
            accounts_store=AccountStoreSettings(uri=mongo_uri, database=database),
            local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
        ),
        secrets=SecretsSettings(provider="file", path=str(secrets_dir)),
        api=APISettings(jwt_secret="test-secret-key-for-unit-tests-hmac32"),
    )


@pytest.fixture
def engines(mongo_uri: str, tmp_path: Path) -> Iterator[SimpleNamespace]:
    """Engines ``a`` and ``b`` on one throwaway database, with an admin's bearer header."""
    database = f"dfe_engine_test_{uuid.uuid4().hex[:8]}"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    first = _settings(tmp_path / "a", secrets_dir, mongo_uri, database)
    second = _settings(tmp_path / "b", secrets_dir, mongo_uri, database)
    app_a, app_b = create_app(settings=first), create_app(settings=second)
    token = create_access_token(data={"sub": "admin"}, settings=first)
    try:
        with (
            TestClient(app_a, raise_server_exceptions=False) as a,
            TestClient(app_b, raise_server_exceptions=False) as b,
        ):
            admin_on_its_own_password(app_a)
            yield SimpleNamespace(
                a=a, b=b, apps=(app_a, app_b), headers={"Authorization": f"Bearer {token}"}
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
