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

from tests.unit.test_api.test_ingest_rules_api import _wire

SHARED = {"main", "shared-source"}
SCHEMA = {"meta_schema": "meta/aws_cloudtrail", "meta_schema_version": "1.0.0"}
FETCHERS = {"acme-fetch": "test_org", "other-fetch": "test_org_2", "default-fetch": None}


def _store_sources(client, headers, sample_source) -> None:
    """The shared source and three fetcher sources, one per org and one on the default org."""
    created = [
        client.post(
            "/api/v1/sources",
            json={**sample_source, "source": "shared-source"},
            headers=headers,
        )
    ]
    for name, org in FETCHERS.items():
        fetcher = {"source_type": "okta", "topic": "own", **({"org": org} if org else {})}
        body = {
            **sample_source,
            "source": name,
            "match": None,
            "fetcher": fetcher,
            "schema_config": SCHEMA,
        }
        body = {k: v for k, v in body.items() if v is not None}
        created.append(client.post("/api/v1/sources", json=body, headers=headers))
    assert [resp.status_code for resp in created] == [201] * 4, [r.text for r in created]


@pytest.fixture
def bus_deployment(client, app, api_settings, monkeypatch):
    """A bus deployment with the two orgs the fixture identities hold."""
    monkeypatch.setattr("dfe_engine.settings.get_settings", lambda: api_settings)
    api_settings.transport.bus_present = True
    api_settings.transport.default = "bus"
    for org in ("test_org", "test_org_2"):
        app.state.org_registry.create(org, org_ids=[org])


@pytest.fixture
def stored_sources(bus_deployment, client, admin_headers, sample_source):
    _store_sources(client, admin_headers, sample_source)


@pytest.fixture
def wired_sources(bus_deployment, client, app, admin_headers, sample_source, tmp_path):
    """The same sources over a deploy repo, where the ingest rules live."""
    _wire(app, tmp_path)
    _store_sources(client, admin_headers, sample_source)


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


EVERY_SOURCE = SHARED - {"main"} | set(FETCHERS)

# Who may open which source: a system reader all of them, a held reader the shared
# ones and the fetcher sources of the orgs it holds, a caller with no grant none.
OPENABLE = [
    pytest.param("dfe-admin", EVERY_SOURCE, id="system-admin"),
    pytest.param("dfe-viewer", EVERY_SOURCE, id="system-data-viewer"),
    pytest.param("dfe-test-org-viewer", {"shared-source", "acme-fetch"}, id="org-viewer-own-org"),
    pytest.param(
        "dfe-multi-viewer",
        {"shared-source", "acme-fetch", "other-fetch"},
        id="viewer-of-two-orgs",
    ),
    pytest.param("dfe-nobody", set(), id="no-grant"),
]

NAMED_READS = [
    "/api/v1/sources/{name}",
    "/api/v1/sources/{name}/columns",
    "/api/v1/sources/{name}/flow",
    "/api/v1/sources/{name}/signals",
    "/api/v1/sources/{name}/export",
    "/api/v1/sources/{name}/versions/1.0.0",
    "/api/v1/schemas/{name}/columns",
    "/api/v1/kafka/topics?source={name}",
]
UNNAMED_READS = [
    "/api/v1/sources/catalogue",
    "/api/v1/sources/catalogue-tags",
    "/api/v1/sources/engines",
    "/api/v1/ingest/compat",
    "/api/v1/ingest/compat-profiles",
    "/api/v1/ingest/compat-profiles/dfe-legacy-2x",
]


@pytest.fixture
def composed_columns(monkeypatch):
    """Answer the schema-backed reads with no schema files on disk: no columns, no pinned definitions."""
    from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2

    monkeypatch.setattr(
        SchemaBuilderV2, "version_snapshot_has_schema_files", staticmethod(lambda _snap: True)
    )
    monkeypatch.setattr(SchemaBuilderV2, "load_columns_for_source_version", lambda *_a, **_k: [])
    monkeypatch.setattr("dfe_engine.exchange.sources._pinned_definitions", lambda *_a, **_k: [])


@pytest.mark.usefixtures("composed_columns")
@pytest.mark.parametrize("route", NAMED_READS)
@pytest.mark.parametrize(("identity", "openable"), OPENABLE)
def test_a_source_read_opens_for_a_caller_exactly_where_the_list_shows_it(
    client_as, wired_sources, identity, openable, route
):
    caller = client_as(identity)

    statuses = {name: caller.get(route.format(name=name)).status_code for name in EVERY_SOURCE}

    assert statuses == {name: 200 if name in openable else 403 for name in EVERY_SOURCE}


@pytest.mark.parametrize("route", UNNAMED_READS)
@pytest.mark.parametrize(
    ("identity", "status"),
    [
        pytest.param("dfe-admin", 200, id="system-admin"),
        pytest.param("dfe-test-org-viewer", 200, id="org-viewer"),
        pytest.param("dfe-nobody", 403, id="no-grant"),
    ],
)
def test_the_shared_reference_reads_follow_source_read_at_any_scope(
    client_as, wired_sources, identity, status, route
):
    resp = client_as(identity).get(route)

    assert resp.status_code == status, resp.text


def test_an_unknown_source_is_a_404_not_a_403_for_an_org_viewer(client_as, stored_sources):
    resp = client_as("dfe-test-org-viewer").get("/api/v1/sources/no-such-source")

    assert resp.status_code == 404, resp.text


@pytest.mark.parametrize(("identity", "openable"), OPENABLE)
def test_the_ingest_rule_list_leaves_out_the_blocks_of_sources_the_caller_cannot_read(
    client_as, wired_sources, identity, openable
):
    resp = client_as(identity).get("/api/v1/ingest/rules", params={"per_page": 100})

    if not openable:
        assert resp.status_code == 403, resp.text
        return
    assert resp.status_code == 200, resp.text
    listed = {block["name"] for block in resp.json()["items"] if block["kind"] == "source"}
    assert listed & EVERY_SOURCE == openable


@pytest.fixture
def topic_status_without_a_broker(monkeypatch):
    """Answer the topic status from the specs asked about, as test_kafka_topics does."""
    import dfe_engine.api.v1.kafka_topics as kafka_topics_module
    from dfe_engine.kafka.topics import TopicState, TopicStatusResult

    def _status(specs, *, sources=None, **_kwargs):
        return TopicStatusResult(
            topics=[
                TopicState(
                    name=spec.name,
                    source=(sources or {}).get(spec.name, ""),
                    exists=True,
                    desired_partitions=spec.partitions,
                    desired_replication_factor=spec.replication_factor,
                    partitions=spec.partitions,
                    replication_factor=spec.replication_factor,
                )
                for spec in specs
            ]
        )

    monkeypatch.setattr(kafka_topics_module, "topic_status", _status)


@pytest.mark.parametrize(("identity", "openable"), OPENABLE)
def test_the_topic_status_covers_only_the_sources_the_caller_can_read(
    client_as, stored_sources, topic_status_without_a_broker, identity, openable
):
    resp = client_as(identity).get("/api/v1/kafka/topics")

    if not openable:
        assert resp.status_code == 403, resp.text
        return
    assert resp.status_code == 200, resp.text
    covered = {topic["source"] for topic in resp.json()["topics"]} & EVERY_SOURCE
    assert covered == openable


@pytest.fixture
def hyperdx_teams(app, monkeypatch):
    """A HyperDX that answers with one team, so a permitted read is a 200."""
    app.state.hyperdx_client = object()

    async def _listing(_client):
        return [{"team": "t1", "teamName": "test_org", "sources": []}]

    monkeypatch.setattr("dfe_engine.hyperdx.sources.list_sources_by_team", _listing)


@pytest.mark.parametrize(
    ("identity", "status"),
    [
        pytest.param("dfe-admin", 200, id="system-admin"),
        pytest.param("dfe-viewer", 200, id="system-data-viewer"),
        pytest.param("dfe-test-org-viewer", 403, id="org-viewer-own-org"),
        pytest.param("dfe-multi-viewer", 403, id="viewer-of-two-orgs"),
        pytest.param("dfe-nobody", 403, id="no-grant"),
    ],
)
def test_the_hyperdx_source_listing_stays_with_callers_that_read_every_org(
    client_as, hyperdx_teams, identity, status
):
    resp = client_as(identity).get("/api/v1/hyperdx/sources")

    assert resp.status_code == status, resp.text
