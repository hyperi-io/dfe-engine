#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_state.py
#  Purpose:      Tests for ServiceStateClient and _parse_prometheus_text
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for state.py — pure logic only. HTTP/async methods skipped."""

from __future__ import annotations

import pytest

from dfe_engine.services.state import (
    HealthStatus,
    ServiceStateClient,
    ServiceStatus,
    _parse_prometheus_text,
)

# ---------------------------------------------------------------------------
# _parse_prometheus_text — pure function, fully testable
# ---------------------------------------------------------------------------


class TestParsePrometheusText:
    def test_empty_string_returns_empty_dict(self):
        assert _parse_prometheus_text("") == {}

    def test_blank_lines_skipped(self):
        result = _parse_prometheus_text("\n\n\n")
        assert result == {}

    def test_comment_lines_skipped(self):
        text = "# HELP my_metric A metric\n# TYPE my_metric gauge\n"
        assert _parse_prometheus_text(text) == {}

    def test_single_metric_no_labels(self):
        result = _parse_prometheus_text("my_metric 42.0\n")
        assert result == {"my_metric": 42.0}

    def test_integer_value_parsed_as_float(self):
        result = _parse_prometheus_text("my_counter 100\n")
        assert result == {"my_counter": 100.0}
        assert isinstance(result["my_counter"], float)

    def test_metric_with_labels(self):
        # Labels in braces should be ignored; name extracted correctly
        result = _parse_prometheus_text('http_requests_total{method="GET",code="200"} 1234\n')
        assert result["http_requests_total"] == 1234.0

    def test_metric_with_timestamp_not_supported(self):
        # The regex only captures name + value; timestamp is not matched
        # and the metric should still parse (timestamp after value is not in the pattern)
        result = _parse_prometheus_text("my_metric 99.0 1234567890\n")
        # This should still match because the pattern captures just up to the value
        assert "my_metric" in result

    def test_float_scientific_notation(self):
        result = _parse_prometheus_text("my_metric 1.5e3\n")
        assert result["my_metric"] == 1500.0

    def test_multiple_metrics(self):
        text = (
            "# HELP dfe_records_received_total Total received\n"
            "# TYPE dfe_records_received_total counter\n"
            "dfe_records_received_total 5000\n"
            "# HELP dfe_scaling_pressure Scaling pressure\n"
            "# TYPE dfe_scaling_pressure gauge\n"
            "dfe_scaling_pressure 0.42\n"
        )
        result = _parse_prometheus_text(text)
        assert result["dfe_records_received_total"] == 5000.0
        assert result["dfe_scaling_pressure"] == pytest.approx(0.42)

    def test_metric_name_with_colons(self):
        # Prometheus allows colons in metric names
        result = _parse_prometheus_text("namespace:subsystem:metric_total 7\n")
        assert result["namespace:subsystem:metric_total"] == 7.0

    def test_last_value_wins_for_duplicate_names(self):
        # When the same metric name appears multiple times (different labels),
        # the last value wins (regex matches the name without labels)
        text = 'http_requests{method="GET"} 100\nhttp_requests{method="POST"} 200\n'
        result = _parse_prometheus_text(text)
        assert result["http_requests"] == 200.0

    def test_mixed_content_with_comments(self):
        text = (
            "# HELP up Service up indicator\n"
            "# TYPE up gauge\n"
            "up 1\n"
            "\n"
            "# HELP process_start_time_seconds Start time\n"
            "process_start_time_seconds 1.7e9\n"
        )
        result = _parse_prometheus_text(text)
        assert result["up"] == 1.0
        assert result["process_start_time_seconds"] == 1.7e9

    def test_line_with_leading_whitespace_stripped(self):
        result = _parse_prometheus_text("  my_metric 55.0\n")
        assert result["my_metric"] == 55.0

    def test_returns_dict_type(self):
        result = _parse_prometheus_text("a 1\n")
        assert isinstance(result, dict)

    def test_no_match_line_ignored(self):
        # A line that doesn't match the pattern is silently skipped
        result = _parse_prometheus_text("not-a-valid-metric-line\n")
        assert result == {}


# ---------------------------------------------------------------------------
# HealthStatus model
# ---------------------------------------------------------------------------


class TestHealthStatus:
    def test_defaults(self):
        hs = HealthStatus(alive=True, ready=False)
        assert hs.alive is True
        assert hs.ready is False
        assert hs.details == {}

    def test_with_details(self):
        hs = HealthStatus(alive=True, ready=True, details={"clickhouse": "ok"})
        assert hs.details["clickhouse"] == "ok"

    def test_serialization(self):
        hs = HealthStatus(alive=True, ready=True)
        d = hs.model_dump()
        assert d == {"alive": True, "ready": True, "details": {}}

    def test_alive_false(self):
        hs = HealthStatus(alive=False, ready=False)
        assert not hs.alive


# ---------------------------------------------------------------------------
# ServiceStatus model
# ---------------------------------------------------------------------------


class TestServiceStatus:
    def test_construction(self):
        health = HealthStatus(alive=True, ready=True)
        status = ServiceStatus(
            service="loader",
            instance="production",
            alive=True,
            ready=True,
            health=health,
        )
        assert status.service == "loader"
        assert status.instance == "production"
        assert status.alive is True
        assert status.ready is True
        assert status.metrics == {}
        assert status.config_version is None

    def test_with_metrics_and_version(self):
        health = HealthStatus(alive=True, ready=True)
        status = ServiceStatus(
            service="receiver",
            instance="default",
            alive=True,
            ready=True,
            health=health,
            metrics={"up": 1.0, "dfe_records_received_total": 500.0},
            config_version=3,
        )
        assert status.metrics["up"] == 1.0
        assert status.config_version == 3

    def test_serialization(self):
        health = HealthStatus(alive=False, ready=False)
        status = ServiceStatus(
            service="archiver",
            instance="staging",
            alive=False,
            ready=False,
            health=health,
        )
        d = status.model_dump()
        assert d["service"] == "archiver"
        assert d["alive"] is False


# ---------------------------------------------------------------------------
# ServiceStateClient construction and pure methods
# ---------------------------------------------------------------------------


class TestServiceStateClientConstruction:
    def test_default_metrics_url_equals_base_url(self):
        client = ServiceStateClient(
            service="receiver",
            base_url="http://localhost:8080",
        )
        assert client.base_url == "http://localhost:8080"
        assert client.metrics_url == "http://localhost:8080"
        assert client.instance == "default"
        assert client._timeout == 5.0

    def test_explicit_metrics_url(self):
        client = ServiceStateClient(
            service="loader",
            base_url="http://localhost:9090",
            metrics_url="http://localhost:9091",
            instance="prod",
            timeout=10.0,
        )
        assert client.metrics_url == "http://localhost:9091"
        assert client.instance == "prod"
        assert client._timeout == 10.0

    def test_trailing_slash_stripped_from_base_url(self):
        client = ServiceStateClient(
            service="archiver",
            base_url="http://localhost:9090/",
        )
        assert client.base_url == "http://localhost:9090"

    def test_trailing_slash_stripped_from_metrics_url(self):
        client = ServiceStateClient(
            service="archiver",
            base_url="http://localhost:9090",
            metrics_url="http://localhost:9091/",
        )
        assert client.metrics_url == "http://localhost:9091"

    def test_service_name_stored(self):
        client = ServiceStateClient(service="fetcher", base_url="http://localhost:9090")
        assert client.service == "fetcher"


# ---------------------------------------------------------------------------
# ServiceStateClient._liveness_paths and _readiness_paths
# ---------------------------------------------------------------------------


class TestServiceStateClientPaths:
    def test_liveness_paths_receiver(self):
        client = ServiceStateClient(service="receiver", base_url="http://localhost:8080")
        paths = client._liveness_paths()
        assert isinstance(paths, list)
        assert len(paths) > 0
        # Receiver plugin defines /health/live
        assert "/health/live" in paths

    def test_readiness_paths_receiver(self):
        client = ServiceStateClient(service="receiver", base_url="http://localhost:8080")
        paths = client._readiness_paths()
        assert isinstance(paths, list)
        assert len(paths) > 0
        assert "/health/ready" in paths

    def test_liveness_paths_loader(self):
        client = ServiceStateClient(service="loader", base_url="http://localhost:9090")
        paths = client._liveness_paths()
        assert isinstance(paths, list)
        # Loader plugin defines /live and /health
        assert any(p in paths for p in ("/live", "/health"))

    def test_readiness_paths_loader(self):
        client = ServiceStateClient(service="loader", base_url="http://localhost:9090")
        paths = client._readiness_paths()
        assert isinstance(paths, list)
        assert any(p in paths for p in ("/ready", "/health"))

    def test_liveness_paths_unknown_service_returns_metrics_fallback(self):
        client = ServiceStateClient(service="nonexistent-service", base_url="http://localhost:9090")
        paths = client._liveness_paths()
        assert paths == ["/metrics"]

    def test_readiness_paths_unknown_service_returns_metrics_fallback(self):
        client = ServiceStateClient(service="nonexistent-service", base_url="http://localhost:9090")
        paths = client._readiness_paths()
        assert paths == ["/metrics"]

    def test_liveness_paths_archiver(self):
        client = ServiceStateClient(service="archiver", base_url="http://localhost:9090")
        paths = client._liveness_paths()
        assert isinstance(paths, list)
        assert len(paths) > 0

    def test_paths_are_strings(self):
        for svc in ("receiver", "loader", "archiver"):
            client = ServiceStateClient(service=svc, base_url="http://localhost:9090")
            for path in client._liveness_paths():
                assert isinstance(path, str)
                assert path.startswith("/")
