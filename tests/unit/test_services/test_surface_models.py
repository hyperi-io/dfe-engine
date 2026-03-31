"""Tests for service surface Pydantic models."""

from __future__ import annotations

import pytest

from dfe_engine.services.surfaces.models import (
    ConfigSurfaceEntry,
    MetricEntry,
    ServiceSurface,
)


class TestConfigSurfaceEntry:
    def test_defaults(self):
        entry = ConfigSurfaceEntry()
        assert entry.type == "string"
        assert entry.description == ""
        assert entry.default is None

    def test_with_values(self):
        entry = ConfigSurfaceEntry(
            type="integer",
            description="Buffer size limit",
            default=67108864,
        )
        assert entry.type == "integer"
        assert entry.default == 67108864

    def test_boolean_type(self):
        entry = ConfigSurfaceEntry(
            type="boolean",
            description="Enable feature",
            default=False,
        )
        assert entry.type == "boolean"
        assert entry.default is False

    def test_serialise_roundtrip(self):
        entry = ConfigSurfaceEntry(type="float", description="threshold", default=0.95)
        data = entry.model_dump()
        restored = ConfigSurfaceEntry(**data)
        assert restored == entry


class TestMetricEntry:
    def test_required_fields(self):
        metric = MetricEntry(name="dfe_receiver_requests_total", type="counter")
        assert metric.name == "dfe_receiver_requests_total"
        assert metric.type == "counter"
        assert metric.labels == []
        assert metric.group == ""

    def test_full_metric(self):
        metric = MetricEntry(
            name="dfe_loader_insert_duration_seconds",
            type="histogram",
            description="Insert latency",
            unit="seconds",
            labels=["table"],
            group="sink",
        )
        assert metric.unit == "seconds"
        assert metric.labels == ["table"]
        assert metric.group == "sink"

    def test_missing_name_raises(self):
        with pytest.raises(Exception):
            MetricEntry(type="gauge")

    def test_missing_type_raises(self):
        with pytest.raises(Exception):
            MetricEntry(name="some_metric")


class TestServiceSurface:
    def test_minimal_surface(self):
        surface = ServiceSurface(service="dfe-test")
        assert surface.service == "dfe-test"
        assert surface.config_surface == {}
        assert surface.metrics_surface == []
        assert surface.manifest_url == ""
        assert surface.discovered_at == ""

    def test_full_surface(self):
        surface = ServiceSurface(
            service="dfe-receiver",
            description="Ingest service",
            config_surface={
                "config.server.port": ConfigSurfaceEntry(
                    type="integer",
                    description="Listen port",
                    default=8080,
                ),
            },
            metrics_surface=[
                MetricEntry(
                    name="dfe_receiver_requests_total",
                    type="counter",
                    labels=["transport"],
                ),
            ],
            manifest_url="http://localhost:8080/metrics/manifest",
            discovered_at="2026-03-31T12:00:00Z",
        )
        assert len(surface.config_surface) == 1
        assert len(surface.metrics_surface) == 1
        assert surface.manifest_url.endswith("/manifest")

    def test_json_roundtrip(self):
        surface = ServiceSurface(
            service="dfe-loader",
            config_surface={
                "config.buffer.max_bytes": ConfigSurfaceEntry(
                    type="integer",
                    default=67108864,
                ),
            },
            metrics_surface=[
                MetricEntry(name="dfe_loader_rows_inserted_total", type="counter"),
            ],
        )
        data = surface.model_dump(mode="json")
        restored = ServiceSurface(**data)
        assert restored.service == surface.service
        assert len(restored.config_surface) == 1
        assert len(restored.metrics_surface) == 1
