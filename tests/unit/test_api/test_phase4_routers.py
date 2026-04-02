#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_phase4_routers.py
#  Purpose:      Tests for Phase 4 routers (discovery, sigma, schemas)
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for Phase 4: discovery, sigma, and schemas routers.

These tests exercise the router-level behaviour without real ClickHouse.
All CH-dependent endpoints return 503 (not configured) by design — the
routers guard against missing infrastructure cleanly.
"""

from __future__ import annotations


class TestDiscoveryRouter:
    """GET /api/v1/discovery endpoints."""

    def test_databases_no_clickhouse_returns_503(self, client, admin_headers):
        """Returns 503 when ClickHouse is not reachable."""
        resp = client.get("/api/v1/discovery/databases", headers=admin_headers)
        assert resp.status_code == 503
        assert resp.json()["code"] in ("not_configured", "connection_error")

    def test_tables_no_clickhouse_returns_503(self, client, admin_headers):
        resp = client.get("/api/v1/discovery/tables", headers=admin_headers)
        assert resp.status_code == 503

    def test_columns_no_clickhouse_returns_503(self, client, admin_headers):
        resp = client.get(
            "/api/v1/discovery/tables/my_table/columns",
            headers=admin_headers,
        )
        assert resp.status_code == 503

    def test_requires_auth(self, client):
        resp = client.get("/api/v1/discovery/databases")
        assert resp.status_code == 401


class TestSigmaRouter:
    """GET/POST /api/v1/sigma endpoints."""

    def test_mappings_source_not_found(self, client, admin_headers):
        """Returns 503 or 404 when source not configured/found."""
        resp = client.get("/api/v1/sigma/mappings/nonexistent", headers=admin_headers)
        # 503 if SourceRegistry not available for sigma, or 404 if source missing
        assert resp.status_code in (404, 503)

    def test_generate_view_source_not_found(self, client, admin_headers):
        resp = client.post("/api/v1/sigma/views/nonexistent", headers=admin_headers)
        assert resp.status_code in (404, 503)

    def test_generate_all_views(self, client, admin_headers):
        """Generate all views — may return empty list or 503."""
        resp = client.post("/api/v1/sigma/views", headers=admin_headers)
        # Either succeeds with empty list (no sources) or 503 (not configured)
        assert resp.status_code in (200, 503)

    def test_logsource_search(self, client, admin_headers):
        """Find sources for logsource — returns list (possibly empty)."""
        resp = client.get(
            "/api/v1/sigma/logsource?product=windows&category=process_creation",
            headers=admin_headers,
        )
        assert resp.status_code in (200, 503)

    def test_requires_auth(self, client):
        resp = client.get("/api/v1/sigma/mappings/test")
        assert resp.status_code == 401


class TestSchemasRouter:
    """GET/POST /api/v1/schemas endpoints."""

    def test_columns_source_not_found(self, client, admin_headers):
        """Returns 404 when source doesn't exist."""
        resp = client.get("/api/v1/schemas/nonexistent/columns", headers=admin_headers)
        assert resp.status_code == 404

    def test_build_source_not_found(self, client, admin_headers):
        """Returns 404 when source doesn't exist."""
        resp = client.post("/api/v1/schemas/nonexistent/build", headers=admin_headers)
        assert resp.status_code == 404

    def test_columns_with_existing_source(self, client, admin_headers, sample_source):
        """Create a source, then try to get columns (may 404 if no schema path)."""
        # First create a source
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.get(
            f"/api/v1/schemas/{sample_source['source']}/columns",
            headers=admin_headers,
        )
        # Source exists but likely has no schema file → 404 (no_schema)
        assert resp.status_code in (200, 404)
        if resp.status_code == 404:
            assert resp.json()["code"] in ("not_found", "no_schema")

    def test_requires_auth(self, client):
        resp = client.get("/api/v1/schemas/test/columns")
        assert resp.status_code == 401
