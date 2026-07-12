"""Tests for `dfe kafka client-config` - the tool-config emission command.

The scalo emitter (scalo.kafka.toolconfig) is exercised in scalo-py; here we stub
it and test the CLI's OWN behaviour: cred sourcing, the 0600 file write, the
eval-export shape, and the clean error when scalo lacks the emitter.
"""

from __future__ import annotations

import builtins
import sys
import types
from dataclasses import dataclass

import pytest
from click.testing import CliRunner

from dfe_engine.cli.auto.kafka import kafka_group


@pytest.fixture
def stub_toolconfig(monkeypatch):
    """Inject a fake scalo.kafka.toolconfig (the real one ships once scalo bumps)."""
    mod = types.ModuleType("scalo.kafka.toolconfig")

    @dataclass
    class Connection:
        bootstrap_servers: str
        username: str = ""
        password: str = ""
        schema_registry_url: str = ""

    def emit_librdkafka_properties(provider, conn):
        lines = [
            f"bootstrap.servers={conn.bootstrap_servers}",
            "security.protocol=SASL_SSL",
            "sasl.mechanisms=SCRAM-SHA-512",
            f"sasl.username={conn.username}",
            f"sasl.password={conn.password}",
        ]
        return "\n".join(lines) + "\n"

    def emit_kcat(provider, conn):
        return emit_librdkafka_properties(provider, conn)

    def emit_kafbat_cluster(provider, conn):
        return {"name": provider, "bootstrapServers": conn.bootstrap_servers, "properties": {}}

    mod.Connection = Connection
    mod.emit_librdkafka_properties = emit_librdkafka_properties
    mod.emit_kcat = emit_kcat
    mod.emit_kafbat_cluster = emit_kafbat_cluster
    monkeypatch.setitem(sys.modules, "scalo.kafka.toolconfig", mod)
    return mod


def test_properties_to_stdout(stub_toolconfig):
    r = CliRunner().invoke(
        kafka_group,
        [
            "client-config",
            "--provider",
            "redpanda",
            "--bootstrap",
            "redpanda:9093",
            "--username",
            "svc",
            "--password",
            "pw",
        ],
    )
    assert r.exit_code == 0, r.output
    assert "bootstrap.servers=redpanda:9093" in r.output
    assert "sasl.mechanisms=SCRAM-SHA-512" in r.output
    assert "sasl.username=svc" in r.output


def test_out_writes_0600(stub_toolconfig, tmp_path):
    out = tmp_path / "kcat.conf"
    r = CliRunner().invoke(
        kafka_group,
        [
            "client-config",
            "--provider",
            "redpanda",
            "--bootstrap",
            "b:9093",
            "--username",
            "u",
            "--password",
            "p",
            "--out",
            str(out),
        ],
    )
    assert r.exit_code == 0, r.output
    assert out.exists()
    assert oct(out.stat().st_mode & 0o777) == "0o600"


def test_eval_exports_kcat_config(stub_toolconfig):
    r = CliRunner().invoke(
        kafka_group,
        [
            "client-config",
            "--provider",
            "confluent-cloud",
            "--bootstrap",
            "b:9092",
            "--username",
            "k",
            "--password",
            "s",
            "--eval",
        ],
    )
    assert r.exit_code == 0, r.output
    assert r.stdout.startswith("export KCAT_CONFIG=")


def test_clean_error_when_scalo_emitter_absent(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "scalo.kafka.toolconfig":
            raise ImportError("no toolconfig in this scalo")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    r = CliRunner().invoke(
        kafka_group,
        ["client-config", "--provider", "redpanda", "--bootstrap", "b:9092"],
    )
    assert r.exit_code != 0
    assert "scalo" in r.output.lower()
