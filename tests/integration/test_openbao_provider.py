#  Project:      dfe-engine
#  File:         tests/integration/test_openbao_provider.py
#  Purpose:      Live OpenBao test of the secrets seam (real provider, no mocks)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The minted-secret seam against a real OpenBao (the k8s backend).

Spins a dev-mode OpenBao via docker on DFE_TEST_DOCKER_HOST (ssh) or local docker,
proves the scalo openbao provider + DfeSecrets wrapper round-trip, and tears down.
Skips if no docker host is reachable. Mirrors the ClickHouse harness pattern.
"""

from __future__ import annotations

import os
import subprocess
import time
import urllib.request
import uuid

import pytest

from dfe_engine.secrets import DfeSecrets

_IMAGE = os.environ.get("DFE_TEST_OPENBAO_IMAGE", "openbao/openbao:2.5.4")
_PORT = int(os.environ.get("DFE_TEST_OPENBAO_PORT", "18200"))


def _docker_prefix(spec: str) -> list[str]:
    if spec in ("local", "localhost", "127.0.0.1", ""):
        return ["docker"]
    return ["ssh", spec.removeprefix("ssh://"), "docker"]


def _reach(spec: str) -> str:
    if spec in ("local", "localhost", "127.0.0.1", ""):
        return "127.0.0.1"
    return spec.removeprefix("ssh://").split("@")[-1]


@pytest.fixture
def openbao_addr():
    spec = os.environ.get("DFE_TEST_DOCKER_HOST", "local")
    docker = _docker_prefix(spec)
    name = f"dfe-bao-test-{uuid.uuid4().hex[:8]}"
    addr = f"http://{_reach(spec)}:{_PORT}"
    try:
        subprocess.run(
            [
                *docker,
                "run",
                "-d",
                "--name",
                name,
                "--cap-add=IPC_LOCK",
                "-p",
                f"{_PORT}:8200",
                "-e",
                "BAO_DEV_ROOT_TOKEN_ID=root",
                "-e",
                f"BAO_API_ADDR={addr}",  # dev advertises this; must be reachable
                _IMAGE,
                "server",
                "-dev",
                "-dev-listen-address=0.0.0.0:8200",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        pytest.skip(f"cannot start OpenBao on docker host '{spec}': {exc}")

    def teardown() -> None:
        try:
            subprocess.run(
                [*docker, "rm", "-f", name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            pass

    ready = False
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"{addr}/v1/sys/health", timeout=2) as r:  # noqa: S310
                if r.status == 200:
                    ready = True
                    break
        except Exception:  # still starting
            time.sleep(1)
    if not ready:
        teardown()
        pytest.skip("OpenBao did not become ready")
    try:
        yield addr
    finally:
        teardown()


def _secrets(addr: str) -> DfeSecrets:
    from scalo.secrets import SecretsManager

    mgr = SecretsManager.from_config(
        {
            "cache": {"enabled": False},
            "openbao": {
                "address": addr,
                "skip_verify": True,
                "auth": {"method": "token", "token": "root"},
            },
        }
    )
    # dev-mode KV v2 is mounted at "secret"
    return DfeSecrets(mgr, provider="openbao", root="", mount="secret")


def test_openbao_round_trip(openbao_addr):
    sec = _secrets(openbao_addr)
    sec.put("dfe/groups/soc-ro", "hunter2")
    assert sec.get("dfe/groups/soc-ro") == "hunter2"
    # rotation (upsert)
    sec.put("dfe/groups/soc-ro", "rotated")
    assert sec.get("dfe/groups/soc-ro") == "rotated"
    assert sec.exists("dfe/groups/soc-ro") is True
    sec.delete("dfe/groups/soc-ro")
    assert sec.exists("dfe/groups/soc-ro") is False
