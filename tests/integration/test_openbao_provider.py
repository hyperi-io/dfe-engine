#  Project:      dfe-engine
#  File:         tests/integration/test_openbao_provider.py
#  Purpose:      Live OpenBao test of the secrets seam (real provider, no mocks)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The minted-secret seam against a real OpenBao (the k8s backend), on the ``openbao`` fixture."""

from dfe_engine.secrets import build_secrets
from dfe_engine.settings import SecretsSettings


def test_openbao_round_trip(openbao, monkeypatch):
    monkeypatch.setenv("VAULT_TOKEN", openbao.token)
    sec = build_secrets(
        SecretsSettings(provider="openbao", addr=openbao.address, mount=openbao.kv_mount())
    )

    sec.put("dfe/groups/soc-ro", "hunter2")
    assert sec.get("dfe/groups/soc-ro") == "hunter2"
    # rotation (upsert)
    sec.put("dfe/groups/soc-ro", "rotated")
    assert sec.get("dfe/groups/soc-ro") == "rotated"
    assert sec.exists("dfe/groups/soc-ro") is True
    sec.delete("dfe/groups/soc-ro")
    assert sec.exists("dfe/groups/soc-ro") is False
