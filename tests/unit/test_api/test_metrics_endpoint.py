"""/metrics must NOT be on the public API port (#106).

Phase 1 mounted /metrics on the FastAPI app because scalo collected metrics but
mounted no endpoint, so scrapes 404'd. Phase 2 moves it to scalo's dedicated
observability port (9090) via ServiceApp, because on the API port it was an
UNAUTHENTICATED metrics surface reachable through the ingress. This test locks
that in: the traffic port must not serve it, so it cannot regress to the
public-exposure state. The obs-port /metrics is scalo's own, tested in scalo.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


class TestMetricsNotOnTrafficPort:
    def test_metrics_is_not_served_on_the_api_port(self, client: TestClient) -> None:
        # 404 (or 405) -- anything but a 200 scrape. The metrics live on 9090.
        resp = client.get("/metrics")
        assert resp.status_code != 200, (
            "/metrics must not be exposed on the public API port (#106)"
        )

    def test_metrics_is_not_api_surface(self, client: TestClient) -> None:
        assert "/metrics" not in client.get("/openapi.json").json()["paths"]
