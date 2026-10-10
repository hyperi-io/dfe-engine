#  Project:      dfe-engine
#  File:         fastapi_telemetry.py
#  Purpose:      FastAPI telemetry settings shared by every app the engine image serves
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""FastAPI telemetry settings for the engine API and the KEDA shim.

scalo builds each process's OTLP exporters from ``OTEL_EXPORTER_OTLP_*``, on the
protocol that endpoint speaks (gRPC on 4317 by default). FastAPI's automatic
setup reads the same endpoint, assumes http/protobuf whatever the port, and adds
a second exporter for spans, metrics and logs. Against the collector's gRPC port
every one of those exports fails, so it stays off and FastAPI's request telemetry
records into the providers scalo installed.
"""

from fastapi.telemetry import TelemetryConfig

FASTAPI_TELEMETRY: TelemetryConfig = {"auto_configure": False}

__all__ = ["FASTAPI_TELEMETRY"]
