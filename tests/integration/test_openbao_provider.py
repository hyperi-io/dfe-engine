#  Project:      dfe-engine
#  File:         tests/integration/test_openbao_provider.py
#  Purpose:      Live OpenBao test of the secrets interface (real provider, no mocks)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The minted-secret interface against a real OpenBao (the k8s backend), on the ``openbao`` fixture."""

from dfe_engine.secrets import DfeSecrets, build_secrets
from dfe_engine.settings import SecretsSettings


def _secrets(openbao, mount: str) -> DfeSecrets:
    return build_secrets(SecretsSettings(provider="openbao", addr=openbao.address, mount=mount))


def test_an_alert_destination_the_engine_writes_resolves_in_the_runner(
    openbao, monkeypatch, tmp_path
):
    from dfe_engine.hunts.alert import AlertDestination, AlertDestinationRegistry

    monkeypatch.setenv("VAULT_TOKEN", openbao.token)
    mount = openbao.kv_mount()
    engine = AlertDestinationRegistry(
        secrets=_secrets(openbao, mount), directory=str(tmp_path / "alerts")
    )
    engine.add(AlertDestination(name="ops", url="slack://t/b/c"))
    runner = AlertDestinationRegistry(secrets=_secrets(openbao, mount))
    assert runner.resolve("ops") == "slack://t/b/c"

    engine.add(AlertDestination(name="ops", url="slack://t/b/c", enabled=False))
    assert runner.resolve("ops") is None
    assert engine.remove("ops") is True
    engine.close()
    assert "ops" not in runner


def test_openbao_round_trip(openbao, monkeypatch):
    monkeypatch.setenv("VAULT_TOKEN", openbao.token)
    sec = _secrets(openbao, openbao.kv_mount())
    sec.put("dfe/groups/soc-ro", "hunter2")
    assert sec.get("dfe/groups/soc-ro") == "hunter2"
    # rotation (upsert)
    sec.put("dfe/groups/soc-ro", "rotated")
    assert sec.get("dfe/groups/soc-ro") == "rotated"
    assert sec.exists("dfe/groups/soc-ro") is True
    sec.delete("dfe/groups/soc-ro")
    assert sec.exists("dfe/groups/soc-ro") is False
