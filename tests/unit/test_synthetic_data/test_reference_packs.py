#  Project:      dfe-engine
#  File:         tests/unit/test_synthetic_data/test_reference_packs.py
#  Purpose:      Shipped reference sets (syslog/otel/beats) generate coherent streams
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dfe_engine.synthetic_data.schema_source import SchemaEventFactory

FIXED_END = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)

SYSLOG_SEVERITIES = {"info", "notice", "warning", "err", "crit"}
OTEL_SEVERITY_BANDS = {"DEBUG": (5, 8), "INFO": (9, 12), "WARN": (13, 16), "ERROR": (17, 20)}
SSH_MARKERS = ("password for", "publickey for", "Invalid user", "Connection closed", "sshd:session")


def schemas_root() -> Path:
    env_dir = os.getenv("DFE_SCHEMAS_DIR")
    if env_dir and (Path(env_dir) / "meta").is_dir():
        return Path(env_dir)
    return Path(__file__).resolve().parents[3] / "schemas"


def load(relpath: str, seed: int = 42) -> SchemaEventFactory:
    path = schemas_root() / relpath
    if not path.exists():
        pytest.skip(f"reference pack not present: {relpath}")
    return SchemaEventFactory.from_schema(path, seed=seed)


class TestSyslogPack:
    def test_stream_shape(self):
        events = load("meta/syslog.yaml").events(200, end=FIXED_END)
        for event in events:
            json.dumps(event)
            assert event["tags"]["synthetic"] is True
            assert event["severity"] in SYSLOG_SEVERITIES
            assert "{" not in event["message"]
            assert event["message"].strip()
            assert event["timestamp"].endswith("Z")

    def test_app_and_message_cohere(self):
        events = load("meta/syslog.yaml").events(200, end=FIXED_END)
        seen_apps = set()
        for event in events:
            app, message, facility = event["appname"], event["message"], event["facility"]
            seen_apps.add(app)
            if app == "sshd":
                assert any(marker in message for marker in SSH_MARKERS)
                assert facility in ("auth", "authpriv")
            elif app == "cron":
                assert "CMD (" in message
                assert facility == "cron"
            elif app == "sudo":
                assert "sudo" in message or "COMMAND=" in message
                assert facility == "authpriv"
            elif app == "systemd":
                assert facility == "daemon"
            elif app == "nginx":
                assert "upstream" in message
                assert event["severity"] in ("err", "warning")
        # The weighted scenario draw must show variety across 200 events.
        assert {"sshd", "cron", "systemd"} <= seen_apps

    def test_hosts_recur(self):
        events = load("meta/syslog.yaml").events(150, end=FIXED_END)
        hostnames = [e["hostname"] for e in events]
        # An entity pool means the same hosts keep appearing.
        assert len(set(hostnames)) < len(hostnames) / 2

    def test_deterministic(self):
        a = load("meta/syslog.yaml", seed=7).events(30, end=FIXED_END)
        b = load("meta/syslog.yaml", seed=7).events(30, end=FIXED_END)
        assert a == b


class TestOtelLogsPack:
    def test_stream_shape_and_severity_bands(self):
        events = load("meta/otel/logs.yaml").events(200, end=FIXED_END)
        seen_severities = set()
        for event in events:
            json.dumps(event)
            assert event["tags"]["synthetic"] is True
            text = event["severityText"]
            seen_severities.add(text)
            low, high = OTEL_SEVERITY_BANDS[text]
            assert low <= event["severityNumber"] <= high
            assert len(event["traceId"]) == 32
            assert len(event["spanId"]) == 16
            assert "{" not in event["body"]
        assert {"INFO", "WARN"} <= seen_severities

    def test_body_matches_service_domain(self):
        events = load("meta/otel/logs.yaml").events(200, end=FIXED_END)
        for event in events:
            body, service = event["body"], event["service_name"]
            if body.startswith(("batch flushed", "consumer lag")):
                assert service in ("dfe-loader", "dfe-receiver")
            if body.startswith("auth "):
                assert service == "auth-svc"


class TestBeatsFilebeatPack:
    def test_stream_shape(self):
        events = load("meta/beats/filebeat.yaml").events(200, end=FIXED_END)
        for event in events:
            json.dumps(event)
            assert event["tags"]["synthetic"] is True
            assert event["agent"]["type"] == "filebeat"
            assert event["host"]["name"]
            assert event["@timestamp"].endswith("Z")
            assert "{" not in event["message"]

    def test_module_dataset_file_cohere(self):
        events = load("meta/beats/filebeat.yaml").events(200, end=FIXED_END)
        for event in events:
            module = event["event"]["module"]
            dataset = event["event"]["dataset"]
            path = event["log"]["file"]["path"]
            message = event["message"]
            assert dataset.startswith(module)
            if dataset == "system.auth":
                assert path == "/var/log/auth.log"
                assert event["process"]["name"] in ("sshd", "sudo")
            elif dataset == "nginx.access":
                assert path == "/var/log/nginx/access.log"
                assert "HTTP/1.1" in message
            elif dataset == "nginx.error":
                assert path == "/var/log/nginx/error.log"
                assert "upstream" in message
