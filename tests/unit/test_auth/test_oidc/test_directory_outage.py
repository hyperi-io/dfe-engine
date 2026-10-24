#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_directory_outage.py
#  Purpose:      Each directory adapter reports a 5xx or a timeout as an outage, a refusal as one
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Which login lookup failures each adapter reports as an outage, and which as a refusal.

Only an outage keeps a login's last known groups, so the line between the two is
what stops a user keeping access by declining a consent scope. Every case crosses
the real protocol: a local Okta, Graph or Cloud Identity service answering a 5xx or
a 403, or a listener that accepts the connection and never answers.
"""

from collections.abc import Callable, Iterator

import pytest

from dfe_engine.auth.oidc.adapters.base import LookupFailure, OIDCGroupAdapter
from dfe_engine.auth.oidc.adapters.google import GoogleAdapter
from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider
from tests.support.loopback import CountingListener
from tests.unit.test_auth.test_oidc.local_cloud_identity import LocalCloudIdentity
from tests.unit.test_auth.test_oidc.local_directory import AppTokenEntra, fast_http
from tests.unit.test_auth.test_oidc.local_graph import LocalGraph
from tests.unit.test_auth.test_oidc.local_okta import LocalOkta, okta_adapter

OID = "00000000-0000-0000-0000-0000000a11ce"
ENTRA = OIDCProvider(type="entra_id", issuer="https://login.microsoftonline.com/tid/v2.0")
GOOGLE = OIDCProvider(
    type="google",
    issuer="https://accounts.google.com",
    groups=GroupResolutionConfig(enrich_on_login=True, mode="api"),
)
FAILING = {"error": {"code": "ServiceUnavailable", "status": "UNAVAILABLE"}}
SCOPE_REFUSED = {"error": {"code": 403, "status": "PERMISSION_DENIED"}}


def _entra(base_url: str) -> OIDCGroupAdapter:
    adapter = AppTokenEntra(ENTRA)
    adapter.GRAPH_BASE = base_url
    return adapter


def _google(base_url: str) -> OIDCGroupAdapter:
    adapter = GoogleAdapter(GOOGLE, access_token="user-token")
    adapter.CLOUD_IDENTITY_BASE = base_url
    return adapter


@pytest.fixture
def silent(monkeypatch) -> Iterator[str]:
    """A directory origin that accepts every connection and never answers, and a client that gives up on it."""
    fast_http(monkeypatch)
    listener = CountingListener(hold=True)
    yield f"http://127.0.0.1:{listener.port}"
    listener.close()


def _okta_answering(status: int) -> tuple[str, Callable[[], None]]:
    okta = LocalOkta(pages=[[]])
    okta.fail_page = 0
    okta.fail_status = status
    okta.fail_body = {"errorCode": "E0000006"}
    okta.start()
    return okta.base_url, okta.stop


def _graph_answering(status: int) -> tuple[str, Callable[[], None]]:
    graph = LocalGraph()
    graph.refuse[f"users/{OID}"] = (status, FAILING)
    graph.start()
    return graph.base_url, graph.stop


def _cloud_identity_answering(status: int) -> tuple[str, Callable[[], None]]:
    directory = LocalCloudIdentity()
    for method in ("searchTransitiveGroups", "searchDirectGroups"):
        directory.refuse[method] = (status, SCOPE_REFUSED if status == 403 else FAILING)
    directory.start()
    return directory.base_url, directory.stop


ADAPTERS = {
    "okta": (okta_adapter, _okta_answering, "jane@acme.test"),
    "entra": (_entra, _graph_answering, OID),
    "google": (_google, _cloud_identity_answering, "alice@acme.test"),
}


@pytest.mark.parametrize("provider", sorted(ADAPTERS))
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_a_failing_directory_is_an_outage(provider, status):
    build, serve, directory_id = ADAPTERS[provider]
    base_url, stop = serve(status)
    try:
        failure = await build(base_url).resolve_user_groups(directory_id)
    finally:
        stop()

    assert failure is LookupFailure.OUTAGE


@pytest.mark.parametrize("provider", sorted(ADAPTERS))
async def test_a_directory_that_never_answers_is_an_outage(silent, provider):
    build, _serve, directory_id = ADAPTERS[provider]

    failure = await build(silent).resolve_user_groups(directory_id)

    assert failure is LookupFailure.OUTAGE


@pytest.mark.parametrize(
    ("status", "failure"), [(429, "outage"), (503, "outage"), (401, "refused"), (403, "refused")]
)
async def test_google_logs_a_failed_user_token_lookup_as_what_it_was(audit_events, status, failure):
    """An outage is never logged as Google refusing the lookup."""
    base_url, stop = _cloud_identity_answering(status)
    try:
        await _google(base_url).resolve_user_groups("alice@acme.test")
    finally:
        stop()

    (line,) = [event for event in audit_events if event.get("failure")]
    assert line["failure"] == failure
    assert ("refused" in line["event"]) is (failure == "refused")


@pytest.mark.parametrize("provider", sorted(ADAPTERS))
@pytest.mark.parametrize("status", [401, 403])
async def test_a_directory_that_refuses_is_a_refusal(provider, status):
    build, serve, directory_id = ADAPTERS[provider]
    base_url, stop = serve(status)
    try:
        failure = await build(base_url).resolve_user_groups(directory_id)
    finally:
        stop()

    assert failure is LookupFailure.REFUSED
