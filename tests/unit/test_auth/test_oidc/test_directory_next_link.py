#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_directory_next_link.py
#  Purpose:      A directory's next-page link is followed only on the directory's own origin
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A next-page link naming another origin never receives the directory credential.

Okta's ``Link`` header and Graph's ``@odata.nextLink`` are answers from the
directory, so a misbehaving or compromised one could point the next request, and
the ``Authorization`` header riding on it, anywhere. Each test serves page one
from the configured directory and links page two to a second stand-in.
"""

import json

import pytest

from dfe_engine.auth.oidc.adapters.base import LookupFailure
from dfe_engine.auth.oidc.adapters.entra import EntraAdapter
from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider
from tests.unit.test_auth.test_oidc.local_graph import TENANT, LocalGraph
from tests.unit.test_auth.test_oidc.local_okta import LocalOkta, okta_adapter

OKTA_GROUP = {"id": "00g1", "profile": {"name": "SOC Analysts"}}
GRAPH_GROUP = {"id": "11111111-0000-4000-8000-000000000001", "displayName": "dfe-admins"}
OID = "00000000-0000-4000-8000-0000000000aa"


@pytest.fixture
def foreign_okta():
    server = LocalOkta(pages=[[OKTA_GROUP]])
    server.start()
    yield server
    server.stop()


@pytest.fixture
def okta(foreign_okta: LocalOkta):
    server = LocalOkta(pages=[[OKTA_GROUP], [OKTA_GROUP]])
    server.next_base = foreign_okta.base_url
    server.start()
    yield server
    server.stop()


@pytest.fixture
def foreign_graph():
    server = LocalGraph()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def graph(foreign_graph: LocalGraph):
    server = LocalGraph()
    server.next_base = foreign_graph.base_url
    server.start()
    yield server
    server.stop()


def _entra(server: LocalGraph, *, access_token: str = "") -> EntraAdapter:
    provider = OIDCProvider(
        type="entra_id",
        issuer="https://login.microsoftonline.com/test-tenant/v2.0",
        groups=GroupResolutionConfig(mode="api"),
    )
    adapter = EntraAdapter(provider, access_token=access_token)
    adapter.GRAPH_BASE = server.base_url
    adapter._get_token = lambda: "app-token"  # type: ignore[method-assign]
    return adapter


async def _outcome(call) -> tuple[object, BaseException | None]:
    try:
        return await call, None
    except Exception as exc:
        return None, exc


def _refusal_logged(events: list[dict], token: str) -> bool:
    refused = [e for e in events if "next page" in e["event"] and "origin" in e["event"]]
    return bool(refused) and token not in json.dumps(events, default=str)


class TestOkta:
    async def test_a_listing_stops_at_a_link_to_another_origin(
        self, audit_events: list[dict], foreign_okta: LocalOkta, okta: LocalOkta
    ):
        _result, raised = await _outcome(okta_adapter(okta.base_url).list_all_groups())

        assert foreign_okta.authorizations == []
        assert type(raised).__name__ == "DirectoryForeignLinkError"
        assert okta.pages_requested == [0]
        assert _refusal_logged(audit_events, "test-token")

    async def test_a_login_lookup_stops_at_a_link_to_another_origin(
        self, foreign_okta: LocalOkta, okta: LocalOkta
    ):
        groups, raised = await _outcome(okta_adapter(okta.base_url).resolve_user_groups("jane"))

        assert foreign_okta.authorizations == []
        assert (groups, raised) == (LookupFailure.REFUSED, None)

    async def test_the_same_host_on_another_port_is_another_origin(self, okta: LocalOkta):
        port = int(okta.base_url.rsplit(":", 1)[1])
        okta.next_base = f"http://127.0.0.1:{port + 1}"

        _result, raised = await _outcome(okta_adapter(okta.base_url).list_all_groups())

        assert type(raised).__name__ == "DirectoryForeignLinkError"

    async def test_another_host_on_the_same_port_is_another_origin(self, okta: LocalOkta):
        """``localhost`` reaches this very server, so a host check that passed it would show here."""
        okta.next_base = okta.base_url.replace("127.0.0.1", "localhost")

        _result, raised = await _outcome(okta_adapter(okta.base_url).list_all_groups())

        assert okta.pages_requested == [0]
        assert type(raised).__name__ == "DirectoryForeignLinkError"

    async def test_a_link_on_the_directorys_own_origin_is_followed(self, okta: LocalOkta):
        okta.next_base = None

        groups = await okta_adapter(okta.base_url).list_all_groups()

        assert [g.name for g in groups] == ["SOC Analysts", "SOC Analysts"]
        assert okta.pages_requested == [0, 1]


class TestEntra:
    async def test_a_tenant_listing_stops_at_a_link_to_another_origin(
        self, audit_events: list[dict], foreign_graph: LocalGraph, graph: LocalGraph
    ):
        graph.pages[TENANT] = [[GRAPH_GROUP], [GRAPH_GROUP]]

        _result, raised = await _outcome(_entra(graph).list_all_groups())

        assert foreign_graph.requests == []
        assert type(raised).__name__ == "DirectoryForeignLinkError"
        assert graph.principals == [TENANT]
        assert _refusal_logged(audit_events, "app-token")

    async def test_a_login_lookup_stops_at_a_link_to_another_origin(
        self, foreign_graph: LocalGraph, graph: LocalGraph
    ):
        graph.pages["me"] = [[GRAPH_GROUP], [GRAPH_GROUP]]

        groups, raised = await _outcome(
            _entra(graph, access_token="user-token").resolve_user_groups(OID)
        )

        assert foreign_graph.requests == []
        assert raised is None
        assert groups == []

    async def test_a_link_on_graphs_own_origin_is_followed(self, graph: LocalGraph):
        graph.next_base = None
        graph.pages[TENANT] = [[GRAPH_GROUP], [GRAPH_GROUP]]

        groups = await _entra(graph).list_all_groups()

        assert len(groups) == 2
        assert graph.principals == [TENANT, TENANT]


def test_the_sync_says_the_listing_was_abandoned_for_the_link():
    from dfe_engine.auth.oidc.adapters.base import DirectoryForeignLinkError
    from dfe_engine.auth.oidc.sync import _listing_failure_message

    message = _listing_failure_message(DirectoryForeignLinkError())

    assert "another origin" in message
    assert "abandoned" in message
