#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_oidc_directory_outage_login.py
#  Purpose:      An OIDC login whose directory lookup fails keeps the account's stored groups
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A login whose token carries no groups and whose directory gives no answer.

An Entra overage token carries a pointer at Graph instead of its groups. When Graph
refuses the lookup the callback runs on the account's stored groups, so the login
neither fails nor rewrites them, and its sessions stay. The RP's token handling is
the real one, against the real Entra adapter over the local Graph service.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.rp import NormalizedIdentity, OidcRelyingParty
from tests.unit.test_auth.test_oidc.local_directory import AppTokenEntra
from tests.unit.test_auth.test_oidc.local_graph import LocalGraph

PROVIDER = "acme-entra"
ENTRA = OIDCProvider(type="entra_id", issuer="https://login.microsoftonline.com/tid/v2.0")
OID = "00000000-0000-0000-0000-0000000a11ce"
SUBJECT = "pairwise-sub-alice"
ADMINS_GUID = "0d1e2f30-0000-4000-8000-00000000ad31"

# The claims Authlib hands back for an overage login once it has validated the id_token.
OVERAGE_TOKEN: dict[str, Any] = {
    "userinfo": {
        "_claim_names": {"groups": "src1"},
        "email": "alice@acme.test",
        "name": "Alice",
        "oid": OID,
        "sub": SUBJECT,
    },
}


class _OverageRp(OidcRelyingParty):
    """The real RP token handling, fed a validated overage token instead of an IdP exchange."""

    def has_provider(self, provider_name: str) -> bool:
        return provider_name == PROVIDER

    async def handle_callback(self, provider_name: str, request) -> NormalizedIdentity:
        return await self._identity_from_token(
            client=None, provider=ENTRA, provider_name=provider_name, token=OVERAGE_TOKEN
        )


@pytest.fixture
def graph_refuses(monkeypatch):
    """Graph refusing every lookup of the user, reached through the real Entra adapter."""
    graph = LocalGraph()
    graph.refuse[f"users/{OID}"] = (403, {"error": {"code": "Authorization_RequestDenied"}})
    graph.start()
    adapter = AppTokenEntra(ENTRA)
    adapter.GRAPH_BASE = graph.base_url
    monkeypatch.setattr("dfe_engine.auth.oidc.adapters.get_adapter", lambda *_a, **_k: adapter)
    yield graph
    graph.stop()


@pytest.fixture
def overage_rp(app, client: TestClient, tmp_path):
    app.state.oidc_rp = _OverageRp(OIDCProviderRegistry(tmp_path / "rp-registry"))
    app.state.group_store.create(
        "entra-admins", roles=["admin"], source_provider=PROVIDER, source_id=ADMINS_GUID
    )
    return app


def _seed_account(app) -> Any:
    """Alice's account as her last successful login left it, holding the admins group."""
    accounts = app.state.account_store
    accounts.create(SUBJECT, "", groups=[ADMINS_GUID])
    return accounts.update(SUBJECT, external=True, source_provider=PROVIDER, subject=SUBJECT)


class TestADirectoryOutageAtLogin:
    def test_an_existing_account_logs_in_on_its_stored_groups(
        self, client: TestClient, overage_rp, graph_refuses
    ):
        before = _seed_account(overage_rp)

        resp = client.get(f"/api/v1/auth/oidc/{PROVIDER}/callback")

        assert resp.status_code == 200, resp.text
        assert resp.json()["groups"] == [ADMINS_GUID]
        stored = overage_rp.state.account_store.get(SUBJECT)
        assert stored.groups == [ADMINS_GUID]
        assert stored.session_marker() == before.session_marker()
        assert graph_refuses.principals == [f"users/{OID}"]

    def test_a_first_login_with_nothing_stored_still_gets_no_role(
        self, client: TestClient, overage_rp, graph_refuses
    ):
        resp = client.get(f"/api/v1/auth/oidc/{PROVIDER}/callback")

        assert resp.status_code == 401, resp.text
        assert resp.json()["code"] == "no_role_granted"
        assert overage_rp.state.account_store.get(SUBJECT) is None
