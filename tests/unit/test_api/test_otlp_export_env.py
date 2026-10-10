#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_otlp_export_env.py
#  Purpose:      The deployed OTEL env yields gRPC exporters only, and no http/protobuf one
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The engine's apps export OTLP only on the protocol the deployed endpoint speaks.

The charts set ``OTEL_EXPORTER_OTLP_ENDPOINT`` to the collector's gRPC port and no
protocol. scalo resolves that to gRPC. FastAPI's own setup would read the same
endpoint as http/protobuf, so a real listener stands in for the collector and
records every HTTP request an exporter sends it.
"""

import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from opentelemetry import metrics, trace
from scalo.metrics.opentelemetry_backend import resolve_otel_config
from scalo.otel_tracing import OtelTracingConfig
from scalo.otel_tracing import resolve as resolve_tracing

from dfe_engine.keda_shim.app import create_app as create_shim_app
from dfe_engine.keda_shim.shim import QueryShim
from dfe_engine.settings import DFESettings

FLUSH_MILLIS = 5_000

# Every variable FastAPI or scalo reads to choose an OTLP exporter, protocol or endpoint.
OTEL_SELECTORS = (
    "OTEL_SDK_DISABLED",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_TRACES_EXPORTER",
    "OTEL_METRICS_EXPORTER",
    "OTEL_LOGS_EXPORTER",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
    "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL",
    "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
)


@dataclass(slots=True)
class Collector:
    """A listener on the endpoint the env names, and the paths POSTed to it."""

    endpoint: str
    paths: list[str] = field(default_factory=list)


@pytest.fixture
def collector() -> Iterator[Collector]:
    """A real HTTP listener on a free local port, recording each request path."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Collector(f"http://127.0.0.1:{server.server_address[1]}", seen)
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def deployed_otel_env(monkeypatch, collector: Collector) -> Collector:
    """The chart's OTEL env: an endpoint and nothing else, aimed at the listener."""
    for name in OTEL_SELECTORS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.endpoint)
    return collector


def _flush_global_providers() -> None:
    for provider in (trace.get_tracer_provider(), metrics.get_meter_provider()):
        flush = getattr(provider, "force_flush", None)
        if callable(flush):
            flush(timeout_millis=FLUSH_MILLIS)


def test_scalo_resolves_the_deployed_env_to_grpc_on_that_endpoint(deployed_otel_env):
    tracing = resolve_tracing(OtelTracingConfig())
    metric = resolve_otel_config({})

    assert (tracing.protocol, tracing.endpoint) == ("grpc", deployed_otel_env.endpoint)
    assert (metric.protocol, metric.endpoint) == ("grpc", deployed_otel_env.endpoint)


def test_the_engine_api_adds_no_exporter_of_its_own(deployed_otel_env, app):
    tracer_provider = trace.get_tracer_provider()
    meter_provider = metrics.get_meter_provider()

    with TestClient(app, raise_server_exceptions=False) as client:
        client.get("/livez")
        _flush_global_providers()

    assert deployed_otel_env.paths == []
    assert trace.get_tracer_provider() is tracer_provider
    assert metrics.get_meter_provider() is meter_provider


def _no_clickhouse():
    raise ConnectionRefusedError("no ClickHouse in this test")


def test_the_keda_shim_adds_no_exporter_of_its_own(deployed_otel_env):
    tracer_provider = trace.get_tracer_provider()
    meter_provider = metrics.get_meter_provider()
    settings = DFESettings(env="test")
    shim = QueryShim(settings, client_factory=_no_clickhouse)
    app = create_shim_app(settings=settings, shim=shim)

    with TestClient(app) as client:
        client.get("/keda/hunt-backlog")
        _flush_global_providers()

    assert deployed_otel_env.paths == []
    assert trace.get_tracer_provider() is tracer_provider
    assert metrics.get_meter_provider() is meter_provider
