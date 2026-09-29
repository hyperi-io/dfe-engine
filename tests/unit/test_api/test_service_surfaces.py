"""Tests for the service-surfaces API router."""


class TestListServiceSurfaces:
    def test_list_returns_seeded_surfaces(self, client, admin_headers):
        resp = client.get("/api/v1/service-surfaces", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        # Built-ins are seeded (receiver, loader, archiver)
        assert len(data) >= 3
        names = {s["service"] for s in data}
        assert "dfe-receiver" in names
        assert "dfe-loader" in names
        assert "dfe-archiver" in names

    def test_list_summary_fields(self, client, admin_headers):
        resp = client.get("/api/v1/service-surfaces", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        for item in data:
            assert "service" in item
            assert "config_count" in item
            assert "metrics_count" in item
            assert item["config_count"] > 0
            assert item["metrics_count"] > 0

    def test_list_requires_auth(self, client):
        resp = client.get("/api/v1/service-surfaces")
        assert resp.status_code == 401

    def test_list_viewer_forbidden(self, client, viewer_headers):
        resp = client.get("/api/v1/service-surfaces", headers=viewer_headers)
        assert resp.status_code == 403


class TestGetServiceSurface:
    def test_get_receiver(self, client, admin_headers):
        resp = client.get("/api/v1/service-surfaces/dfe-receiver", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == "dfe-receiver"
        assert "config_surface" in data
        assert "metrics_surface" in data
        assert len(data["config_surface"]) > 0
        assert len(data["metrics_surface"]) > 0

    def test_get_loader(self, client, admin_headers):
        resp = client.get("/api/v1/service-surfaces/dfe-loader", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == "dfe-loader"

    def test_get_archiver(self, client, admin_headers):
        resp = client.get("/api/v1/service-surfaces/dfe-archiver", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == "dfe-archiver"

    def test_get_not_found(self, client, admin_headers):
        resp = client.get("/api/v1/service-surfaces/nonexistent-service", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_get_requires_auth(self, client):
        resp = client.get("/api/v1/service-surfaces/dfe-receiver")
        assert resp.status_code == 401

    def test_get_viewer_forbidden(self, client, viewer_headers):
        resp = client.get("/api/v1/service-surfaces/dfe-receiver", headers=viewer_headers)
        assert resp.status_code == 403


class TestRefreshManifest:
    def test_refresh_not_found(self, client, admin_headers):
        resp = client.post(
            "/api/v1/service-surfaces/nonexistent/metrics/refresh",
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_refresh_with_no_manifest_address_is_not_refreshed(self, client, admin_headers):
        """No DFE_SERVICES_METRICS_MANIFEST_URL here, so nothing is fetched."""
        resp = client.post(
            "/api/v1/service-surfaces/dfe-receiver/metrics/refresh",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == "dfe-receiver"
        assert data["refreshed"] is False
        assert data["discovered_at"] == ""

    def test_refresh_requires_write(self, client, viewer_headers):
        resp = client.post(
            "/api/v1/service-surfaces/dfe-receiver/metrics/refresh",
            headers=viewer_headers,
        )
        assert resp.status_code == 403


class TestSurfaceConfigDetail:
    def test_receiver_config_has_expected_keys(self, client, admin_headers):
        resp = client.get("/api/v1/service-surfaces/dfe-receiver", headers=admin_headers)
        assert resp.status_code == 200
        config = resp.json()["config_surface"]
        assert "config.kafka.bootstrap_servers" in config
        assert "config.server.listen_address" in config

    def test_loader_config_has_clickhouse(self, client, admin_headers):
        resp = client.get("/api/v1/service-surfaces/dfe-loader", headers=admin_headers)
        assert resp.status_code == 200
        config = resp.json()["config_surface"]
        assert "config.clickhouse.host" in config

    def test_archiver_config_has_storage(self, client, admin_headers):
        resp = client.get("/api/v1/service-surfaces/dfe-archiver", headers=admin_headers)
        assert resp.status_code == 200
        config = resp.json()["config_surface"]
        assert "config.storage.backend" in config


class TestSurfaceMetricsDetail:
    def test_receiver_metrics_names(self, client, admin_headers):
        resp = client.get("/api/v1/service-surfaces/dfe-receiver", headers=admin_headers)
        metrics = resp.json()["metrics_surface"]
        metric_names = {m["name"] for m in metrics}
        assert "records_received_total" in metric_names

    def test_loader_metrics_names(self, client, admin_headers):
        resp = client.get("/api/v1/service-surfaces/dfe-loader", headers=admin_headers)
        metrics = resp.json()["metrics_surface"]
        metric_names = {m["name"] for m in metrics}
        assert "rows_inserted_total" in metric_names

    def test_no_surface_names_a_manifest_address_by_default(self, client, admin_headers):
        resp = client.get("/api/v1/service-surfaces", headers=admin_headers)
        assert {s["manifest_url"] for s in resp.json()} == {""}

    def test_metrics_have_type_field(self, client, admin_headers):
        resp = client.get("/api/v1/service-surfaces/dfe-receiver", headers=admin_headers)
        for metric in resp.json()["metrics_surface"]:
            assert metric["type"] in ("counter", "gauge", "histogram")
