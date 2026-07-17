"""The Prometheus scrape endpoint is a deployment contract, so assert it exists.

The charts advertise prometheus.io/scrape + prometheus.io/path: /metrics, and the
engine answered 404: scalo COLLECTS metrics but leaves mounting the endpoint to the
app, and nothing mounted it. It looked instrumented -- scalo logged "Metrics
initialized: backend=prometheus" one line after "Prometheus metrics disabled
(prometheus_client not installed)" -- while exporting nothing at all.

Nothing asserted the contract, so nothing caught it. This does.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


class TestMetricsEndpoint:
    def test_metrics_is_served_in_prometheus_exposition_format(self, client: TestClient) -> None:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        # Prometheus refuses to scrape anything that is not this content type.
        assert resp.headers["content-type"].startswith("text/plain")

    def test_metrics_carries_real_process_samples(self, client: TestClient) -> None:
        # An endpoint that 200s with an empty body would satisfy a naive check while
        # telling the operator nothing -- assert actual samples are present.
        body = client.get("/metrics").text
        assert "python_gc_objects_collected_total" in body

    def test_metrics_is_not_api_surface(self, client: TestClient) -> None:
        # include_in_schema=False: a scrape endpoint is not part of the published API
        # contract, and the OpenAPI spec is a committed artefact (openapi-spec/).
        assert "/metrics" not in client.get("/openapi.json").json()["paths"]
