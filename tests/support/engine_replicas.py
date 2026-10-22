#  Project:      dfe-engine
#  File:         tests/support/engine_replicas.py
#  Purpose:      Whole engine apps sharing one document store, as replicas of one deployment
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Engine replicas over one document-store database and one secrets directory.

Each replica is a whole engine app with its own config directory, on the document
store backend. They share the database, the secrets directory and the auth seed
directory, as the replicas of one deployment share a document store, a secrets
backend and a mounted ConfigMap.
"""

import uuid
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, create_access_token
from dfe_engine.settings import (
    AccountStoreSettings,
    APISettings,
    AuthSettings,
    ClickHouseSettings,
    DFESettings,
    HuntsSettings,
    LocalAuthSettings,
    SchemasSettings,
    SecretsSettings,
    ServicesSettings,
    SigmaSettings,
    SourceSettings,
)
from dfe_engine.store.documents import DocuStore
from tests.support.accounts import admin_on_its_own_password
from tests.support.core_sources import write_landing_definition

ADMIN_PASSWORD = "issued-admin-password-1"


class Deployment:
    """Replicas started through it share one database, secrets directory and seed directory.

    Attributes:
        database: The document-store database every replica uses.
        providers_seed: The ``oidc-providers/`` directory of the shared ``auth.seed_dir``.
        groups_seed: The ``groups/`` directory of the shared ``auth.seed_dir``.
    """

    def __init__(self, *, root: Path, uri: str, stack: ExitStack) -> None:
        self._root = root
        self._uri = uri
        self._stack = stack
        self.database = f"dfe_engine_test_{uuid.uuid4().hex[:8]}"
        self._secrets = root / "secrets"
        self._secrets.mkdir()
        seed = root / "seed"
        self.providers_seed = seed / "oidc-providers"
        self.groups_seed = seed / "groups"
        for directory in (self.providers_seed, self.groups_seed):
            directory.mkdir(parents=True)

    def app(self, name: str) -> FastAPI:
        """An engine app for replica *name*, not yet started."""
        return create_app(settings=self._settings(name))

    def start(self, app: FastAPI) -> TestClient:
        """Run *app*'s startup, which seeds the auth stores, and return its client."""
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()
        self._stack.push(client.__exit__)
        return client

    def replica(self, name: str) -> SimpleNamespace:
        """A started replica, its admin signed in: ``app``, ``client`` and ``headers``."""
        app = self.app(name)
        client = self.start(app)
        admin_on_its_own_password(app)
        token = create_access_token(
            data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
            settings=app.state.settings,
        )
        return SimpleNamespace(app=app, client=client, headers={"Authorization": f"Bearer {token}"})

    def _settings(self, name: str) -> DFESettings:
        root = self._root / name
        dirs = {
            key: root / key for key in ("sources", "services", "rules", "hunts", "auth", "schemas")
        }
        for path in dirs.values():
            path.mkdir(parents=True)
        write_landing_definition(schemas_dir=dirs["schemas"])
        return DFESettings(
            config_dir=str(root),
            clickhouse=ClickHouseSettings(bootstrap_tables=False),
            source=SourceSettings(sources_dir=str(dirs["sources"])),
            services=ServicesSettings(config_yaml_dir=str(dirs["services"])),
            schemas=SchemasSettings(schemas_dir=str(dirs["schemas"])),
            hunts=HuntsSettings(rules_dir=str(dirs["rules"]), hunt_dir=str(dirs["hunts"])),
            sigma=SigmaSettings(enabled=False),
            auth=AuthSettings(
                enabled=True,
                auth_dir=str(dirs["auth"]),
                store_backend="document",
                accounts_store=AccountStoreSettings(uri=self._uri, database=self.database),
                seed_dir=str(self.providers_seed.parent),
                local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
            ),
            secrets=SecretsSettings(provider="file", path=str(self._secrets)),
            api=APISettings(
                jwt_secret="test-secret-key-for-integration-hmac32", jwt_expire_minutes=30
            ),
        )


@contextmanager
def engine_replicas(*, root: Path, uri: str) -> Generator[Deployment]:
    """A :class:`Deployment` whose replicas stop, and whose database is dropped, on exit.

    Args:
        root: An empty directory the replicas' config, secrets and seed live under.
        uri: The document store every replica connects to.
    """
    with ExitStack() as stack:
        deployed = Deployment(root=root, uri=uri, stack=stack)
        try:
            yield deployed
        finally:
            stack.close()
            store = DocuStore(uri, deployed.database)
            store.drop_database(deployed.database)
            store.close()
            _registries.clear()
