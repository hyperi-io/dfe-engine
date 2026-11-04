"""Tests for `dfe kafka client-config` - the tool-config emission command.

These tests stub scalo.kafka.toolconfig to cover credential sourcing, file permissions, shell exports and a missing dependency.
"""

import builtins
import sys
import types
from dataclasses import dataclass

import pytest
from typer.testing import CliRunner

from dfe_engine.cli.auto.app import build_cli


@pytest.fixture(scope="module")
def run():
    """Run ``dfe kafka client-config <args>`` through a CLI built from an empty spec."""
    cli = build_cli({"paths": {}})

    def invoke(*args: str):
        argv = ["kafka", "client-config", *args]
        return CliRunner().invoke(cli.app(argv), argv)

    return invoke


@pytest.fixture
def stub_toolconfig(monkeypatch):
    """Inject a fake scalo.kafka.toolconfig, whose real import needs scalo's kafka extra."""
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

    mod.Connection = Connection  # ty: ignore[unresolved-attribute]  # a bare ModuleType takes any attribute
    mod.emit_librdkafka_properties = emit_librdkafka_properties  # ty: ignore[unresolved-attribute]  # a bare ModuleType takes any attribute
    mod.emit_kcat = emit_kcat  # ty: ignore[unresolved-attribute]  # a bare ModuleType takes any attribute
    mod.emit_kafbat_cluster = emit_kafbat_cluster  # ty: ignore[unresolved-attribute]  # a bare ModuleType takes any attribute
    monkeypatch.setitem(sys.modules, "scalo.kafka.toolconfig", mod)
    return mod


def test_properties_to_stdout(stub_toolconfig, run):
    r = run(
        "--provider",
        "redpanda",
        "--bootstrap",
        "redpanda:9093",
        "--username",
        "svc",
        "--password",
        "pw",
    )
    assert r.exit_code == 0, r.output
    assert "bootstrap.servers=redpanda:9093" in r.stdout
    assert "sasl.mechanisms=SCRAM-SHA-512" in r.stdout
    assert "sasl.username=svc" in r.stdout


def test_out_writes_0600(stub_toolconfig, run, tmp_path):
    out = tmp_path / "kcat.conf"
    r = run(
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
    )
    assert r.exit_code == 0, r.output
    assert oct(out.stat().st_mode & 0o777) == "0o600"


def test_eval_exports_kcat_config(stub_toolconfig, run):
    r = run(
        "--provider",
        "confluent-cloud",
        "--bootstrap",
        "b:9092",
        "--username",
        "k",
        "--password",
        "s",
        "--eval",
    )
    assert r.exit_code == 0, r.output
    assert r.stdout.startswith("export KCAT_CONFIG=")


def test_clean_error_when_scalo_emitter_absent(monkeypatch, run):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "scalo.kafka.toolconfig":
            raise ImportError("no toolconfig in this scalo")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    r = run("--provider", "redpanda", "--bootstrap", "b:9092")
    assert r.exit_code != 0
    assert "scalo.kafka.toolconfig" in r.output
