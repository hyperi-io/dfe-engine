#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_rule_preview_fence.py
#  Purpose:      The alert-volume preview counts only the orgs a rule's hunts and author read
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A rule write's volume preview is held to the orgs its run would read and its author may.

A rule a hunt runs is counted over that hunt's customers, as its compiled statement
reads them. Any rule is held to the orgs its author may read too, so a count never
reports rows from an org the author cannot see.
"""

from types import SimpleNamespace

import pytest

from dfe_engine.hunts.rule_creation_service import RuleCreationService

ORGS = "/api/v1/orgs"
HUNTS = "/api/v1/hunts"
RULES = "/api/v1/rules"
_HUNT = {
    "cron": "*/5 * * * *",
    "rules": ["certutil"],
    "global_source_table_name": "dfe.main",
    "global_target_table_name": "dfe.detection",
}


class _RecordingClickHouse:
    """Parses every rule, and counts 100 events in the window with 2 matches."""

    def __init__(self) -> None:
        self.previews: list[str] = []

    def command(self, *_args, **_kwargs) -> None:
        """Answer the parse check."""

    def query(self, sql: str, **_kwargs) -> SimpleNamespace:
        """Record the preview and answer its two counts."""
        self.previews.append(sql)
        return SimpleNamespace(result_rows=[(100, 2)])


@pytest.fixture
def clickhouse(monkeypatch) -> _RecordingClickHouse:
    recording = _RecordingClickHouse()
    monkeypatch.setattr(RuleCreationService, "_query_client", lambda _self: recording)
    return recording


@pytest.fixture
def acme_hunt(client, admin_headers) -> None:
    """``windows_hunt`` runs ``certutil`` over acme, whose rows carry ``acme-1``."""
    org = client.post(ORGS, json={"name": "acme", "org_ids": ["acme-1"]}, headers=admin_headers)
    assert org.status_code == 201, org.text
    hunt = client.post(
        HUNTS, json={"name": "windows_hunt", **_HUNT, "customers": ["acme"]}, headers=admin_headers
    )
    assert hunt.status_code == 201, hunt.text


def _rule(name: str, **extra) -> dict:
    return {"name": name, "user_sql": "SELECT * FROM dfe.main WHERE severity = 'high'", **extra}


_ACME = "_org_id IN ('acme-1')"


class TestThePreviewCountsWhatTheRunReads:
    def test_a_rule_a_hunt_runs_counts_that_hunts_customers(
        self, client, admin_headers, clickhouse, acme_hunt
    ):
        resp = client.post(RULES, json=_rule("certutil"), headers=admin_headers)

        assert resp.status_code == 201, resp.text
        (sql,) = clickhouse.previews
        assert sql.count(_ACME) == 2

    def test_a_rule_naming_its_hunt_counts_that_hunts_customers(
        self, client, admin_headers, clickhouse, acme_hunt
    ):
        resp = client.post(
            RULES, json=_rule("other", hunt_name="windows_hunt"), headers=admin_headers
        )

        assert resp.status_code == 201, resp.text
        (sql,) = clickhouse.previews
        assert sql.count(_ACME) == 2

    def test_an_update_and_a_hyperdx_view_are_fenced_too(
        self, client, admin_headers, clickhouse, acme_hunt
    ):
        assert client.post(RULES, json=_rule("certutil"), headers=admin_headers).is_success
        update = client.put(f"{RULES}/certutil", json=_rule("certutil"), headers=admin_headers)
        view = client.post(
            f"{RULES}/from-hyperdx",
            json={
                "raw_sql": "SELECT * FROM dfe.main WHERE severity = 'high'",
                "hunt_name": "windows_hunt",
            },
            headers=admin_headers,
        )

        assert update.status_code == 200, update.text
        assert view.status_code == 201, view.text
        assert [sql.count(_ACME) for sql in clickhouse.previews] == [2, 2, 2]

    def test_a_rule_no_hunt_runs_counts_what_its_author_reads(
        self, client, admin_headers, clickhouse, acme_hunt
    ):
        """The admin reads every org, so the count is unfenced."""
        resp = client.post(RULES, json=_rule("lonely"), headers=admin_headers)

        assert resp.status_code == 201, resp.text
        (sql,) = clickhouse.previews
        assert "_org_id" not in sql


class TestAHeldAuthorCountsOnlyItsOwnOrgs:
    @pytest.fixture
    def held(self, monkeypatch) -> None:
        monkeypatch.setattr("dfe_engine.api.v1.rules.held_tenant_ids", lambda *_args: ["t-held"])

    def test_a_rule_no_hunt_runs_counts_the_authors_orgs(
        self, client, admin_headers, clickhouse, acme_hunt, held
    ):
        resp = client.post(RULES, json=_rule("lonely"), headers=admin_headers)

        assert resp.status_code == 201, resp.text
        (sql,) = clickhouse.previews
        assert sql.count("_org_id IN ('t-held')") == 2

    def test_a_hunt_over_none_of_the_authors_orgs_is_left_unmeasured(
        self, client, admin_headers, clickhouse, acme_hunt, held
    ):
        resp = client.post(RULES, json=_rule("certutil"), headers=admin_headers)

        assert resp.status_code == 201, resp.text
        assert clickhouse.previews == []
        assert resp.json()["cost_estimate"]["band"] == "unmeasured"
        assert any("no org" in w for w in resp.json()["rule"]["warnings"])
