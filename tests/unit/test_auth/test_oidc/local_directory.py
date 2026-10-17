#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/local_directory.py
#  Purpose:      A provider directory holding a fixed list of groups, over the local Graph and Okta services
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A provider directory holding a fixed list of groups, served by the local services.

A group sync test needs a directory to list. :func:`graph_directory` and
:func:`okta_directory` start the local Microsoft Graph or Okta service with the groups
loaded and return the real adapter pointed at it, so the listing crosses the real
protocol and the real adapter parses it. :func:`stop_directories` stops every service the
test started, and the OIDC conftest calls it after each test.
"""

from typing import Any

from dfe_engine.auth.oidc.adapters.entra import EntraAdapter
from dfe_engine.auth.oidc.adapters.okta import OktaAdapter
from dfe_engine.auth.oidc.models import GroupInfo, OIDCProvider
from tests.unit.test_auth.test_oidc.local_graph import TENANT, LocalGraph
from tests.unit.test_auth.test_oidc.local_okta import LocalOkta, okta_adapter

_running: list[LocalGraph | LocalOkta] = []


def _graph_item(group: GroupInfo) -> dict[str, Any]:
    """The Graph object for *group*: an unset property is null, and a group with no id has none."""
    item: dict[str, Any] = {
        "description": group.description or None,
        "displayName": group.name or None,
        "mail": group.email or None,
    }
    if group.id:
        item["id"] = group.id
    return item


def graph_directory(provider: OIDCProvider, groups: list[GroupInfo]) -> EntraAdapter:
    """An Entra adapter whose tenant holds *groups*, listed through the local Graph service."""
    graph = LocalGraph()
    graph.pages[TENANT] = [[_graph_item(group) for group in groups]]
    return _entra_over(graph, provider)


def failing_graph_directory(
    provider: OIDCProvider, *, body: dict[str, Any], status: int
) -> EntraAdapter:
    """An Entra adapter whose tenant listing answers *status* and *body*."""
    graph = LocalGraph()
    graph.refuse[TENANT] = (status, body)
    return _entra_over(graph, provider)


class AppTokenEntra(EntraAdapter):
    """Entra with its client-credentials token already in hand, so no MSAL call is made."""

    def _get_token(self) -> str | None:
        return "app-token"


def _entra_over(graph: LocalGraph, provider: OIDCProvider) -> EntraAdapter:
    graph.start()
    _running.append(graph)
    adapter = AppTokenEntra(provider)
    adapter.GRAPH_BASE = graph.base_url
    return adapter


def okta_directory(items: list[dict[str, Any]]) -> OktaAdapter:
    """An Okta adapter whose Management API holds *items*, the group objects Okta serves."""
    okta = LocalOkta(pages=[items])
    okta.start()
    _running.append(okta)
    return okta_adapter(okta.base_url)


def stop_directories() -> None:
    """Stop every local directory service started since the last call."""
    while _running:
        _running.pop().stop()
