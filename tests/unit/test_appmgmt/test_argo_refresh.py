#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_argo_refresh.py
#  Purpose:      The engine asks Argo CD for a hard refresh of the Applications a push changed
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A webhook refresh folded into a running compare leaves a push unseen, so the engine annotates.

The compare itself is Argo's. These tests pin what the engine owes it: the right
PATCH to the right Application after the push, never one for an Application the
push did not write, and never a failure of the push when the cluster refuses.
"""

import json

import httpx
import pytest

from dfe_engine.appmgmt import argo_refresh


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    """A mounted service account and a cluster that records every request it gets."""
    account = tmp_path / "serviceaccount"
    account.mkdir()
    (account / "token").write_text("tkn\n", encoding="utf-8")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        refused = request.url.path.endswith("/dfe-gone-default")
        return httpx.Response(404 if refused else 200, json={})

    return account, seen, httpx.MockTransport(respond)


def test_each_application_gets_one_hard_refresh_patch(cluster):
    account, seen, transport = cluster

    failed = argo_refresh.refresh_applications(
        ["dfe-receiver-default", "dfe-loader-default", "dfe-receiver-default"],
        transport=transport,
        service_account=account,
    )

    assert failed == []
    assert [r.method for r in seen] == ["PATCH", "PATCH"]
    assert [r.url.path for r in seen] == [
        "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/dfe-receiver-default",
        "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/dfe-loader-default",
    ]
    assert seen[0].headers["content-type"] == "application/merge-patch+json"
    assert seen[0].headers["authorization"] == "Bearer tkn"
    assert json.loads(seen[0].content) == {
        "metadata": {"annotations": {"argocd.argoproj.io/refresh": "hard"}}
    }


def test_a_refused_application_is_reported_and_the_rest_still_go(cluster):
    account, seen, transport = cluster

    failed = argo_refresh.refresh_applications(
        ["dfe-gone-default", "dfe-loader-default"], transport=transport, service_account=account
    )

    assert failed == ["dfe-gone-default"]
    assert len(seen) == 2


def test_with_no_service_account_nothing_is_sent(tmp_path, cluster):
    _account, seen, transport = cluster

    failed = argo_refresh.refresh_applications(
        ["dfe-receiver-default"], transport=transport, service_account=tmp_path / "absent"
    )

    assert failed == ["dfe-receiver-default"]
    assert seen == []


def test_nothing_to_refresh_sends_nothing(cluster):
    account, seen, transport = cluster

    assert argo_refresh.refresh_applications([], transport=transport, service_account=account) == []
    assert seen == []
