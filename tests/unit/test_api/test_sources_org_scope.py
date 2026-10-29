#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_sources_org_scope.py
#  Purpose:      GET /sources lists what an org-scoped caller holds, not a system-scope 403
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``GET /api/v1/sources`` for callers whose ``source:read`` is bound to orgs.

A source definition is shared by every org, so every caller sees those. A fetcher
source belongs to the org it names (the default org when it names none), so a caller
held to its orgs lists the fetcher sources of those orgs only.
"""

import pytest

SHARED = {"main", "shared-source"}
FETCHERS = {"acme-fetch": "test_org", "other-fetch": "test_org_2", "default-fetch": None}


@pytest.fixture
def stored_sources(client, app, api_settings, admin_headers, sample_source, monkeypatch):
    """The shared source and three fetcher sources, one per org and one on the default org."""
    monkeypatch.setattr("dfe_engine.settings.get_settings", lambda: api_settings)
    api_settings.transport.bus_present = True
    api_settings.transport.default = "bus"
    for org in ("test_org", "test_org_2"):
        app.state.org_registry.create(org, org_ids=[org])

    created = [
        client.post(
            "/api/v1/sources",
            json={**sample_source, "source": "shared-source"},
            headers=admin_headers,
        )
    ]
    for name, org in FETCHERS.items():
        fetcher = {"source_type": "okta", "topic": "own", **({"org": org} if org else {})}
        body = {
            **sample_source,
            "source": name,
            "match": None,
            "fetcher": fetcher,
            "schema_config": {"meta_schema": "meta/syslog"},
        }
        body = {k: v for k, v in body.items() if v is not None}
        created.append(client.post("/api/v1/sources", json=body, headers=admin_headers))
    assert [resp.status_code for resp in created] == [201] * 4, [r.text for r in created]


def _listed(resp) -> set[str]:
    assert resp.status_code == 200, resp.text
    return {item["name"] for item in resp.json()["items"]}


@pytest.mark.parametrize(
    ("identity", "visible"),
    [
        pytest.param("dfe-admin", SHARED | set(FETCHERS), id="system-admin-sees-all"),
        pytest.param("dfe-viewer", SHARED | set(FETCHERS), id="system-data-viewer-sees-all"),
        pytest.param(
            "dfe-test-org-viewer", SHARED | {"acme-fetch"}, id="org-scoped-viewer-sees-its-org"
        ),
        pytest.param(
            "dfe-multi-viewer",
            SHARED | {"acme-fetch", "other-fetch"},
            id="viewer-of-two-orgs-sees-both",
        ),
    ],
)
def test_a_caller_lists_the_sources_of_the_orgs_it_holds(
    client_as, stored_sources, identity, visible
):
    resp = client_as(identity).get("/api/v1/sources", params={"per_page": 100})

    assert _listed(resp) == visible
    assert resp.json()["total"] == len(visible)


def test_a_caller_with_no_source_read_anywhere_is_refused(client_as, stored_sources):
    resp = client_as("dfe-nobody").get("/api/v1/sources")

    assert resp.status_code == 403, resp.text
    assert "source:read" in resp.json()["message"]
