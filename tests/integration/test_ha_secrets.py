#  Project:      dfe-engine
#  File:         tests/integration/test_ha_secrets.py
#  Purpose:      Engines sharing an OpenBao mint one signing key and read each other's tokens
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""More than one engine on one OpenBao, the shared secrets backend replicas need.

Runs against a dev-mode OpenBao on docker (the ``openbao`` fixture), each module on a
KV v2 mount of its own. The engines are real processes, because one process keeps one
JWT authority per secrets configuration and two apps inside it would share it. They log
in with AppRole on a policy that reaches only their mount, as a deployment's engines do.
"""

import functools
import os
import secrets as random_tokens
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from dfe_engine.auth.jwt_authority import JwtAuthority
from dfe_engine.orgs.ingest_tokens import bearer_secret_source
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import SecretsSettings
from tests.support.races import race, raised

pytestmark = pytest.mark.integration

ISS = "https://dfe.test/api"
RACE_ROUNDS = 20
# An engine imports, boots and mints in a few seconds. A slow docker host gets more.
_START_SECONDS = 120.0


def _versions(openbao, mount: str, path: str) -> int:
    """How many versions OpenBao holds of the KV v2 secret at *path*."""
    return int(openbao.call("GET", f"{mount}/metadata/{path}")["data"]["current_version"])


def _approle(openbao, mount: str) -> tuple[str, str]:
    """A role id and secret id whose tokens reach only *mount*, as a deployment issues them."""
    if "approle/" not in openbao.call("GET", "sys/auth")["data"]:
        openbao.call("POST", "sys/auth/approle", {"type": "approle"})
    role = f"{mount}-engine"
    openbao.call(
        "PUT",
        f"sys/policies/acl/{role}",
        {
            "policy": "\n".join(
                f'path "{mount}/{kind}/*" '
                '{ capabilities = ["create", "read", "update", "delete", "list"] }'
                for kind in ("data", "metadata")
            )
        },
    )
    openbao.call("POST", f"auth/approle/role/{role}", {"token_policies": [role]})
    role_id = openbao.call("GET", f"auth/approle/role/{role}/role-id")["data"]["role_id"]
    secret_id = openbao.call("POST", f"auth/approle/role/{role}/secret-id")["data"]["secret_id"]
    return role_id, secret_id


def test_two_replicas_minting_at_once_store_one_key(openbao, monkeypatch):
    monkeypatch.setenv("VAULT_TOKEN", openbao.token)
    mount = openbao.kv_mount()
    settings = SecretsSettings(provider="openbao", addr=openbao.address, mount=mount)

    for round_ in range(RACE_ROUNDS):
        key_path = f"jwt/race-{round_}"
        # A store each, as each replica builds its own.
        results = race(
            [
                functools.partial(
                    JwtAuthority, build_secrets(settings), issuer=ISS, key_path=key_path
                )
                for _ in range(2)
            ]
        )

        assert raised(results) == [], (round_, results)
        kids = {r.kid for r in results if isinstance(r, JwtAuthority)}
        assert len(kids) == 1, round_
        assert _versions(openbao, mount, key_path) == 1, round_


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_live(url: str, process: subprocess.Popen, log: Path) -> None:
    deadline = time.monotonic() + _START_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"engine at {url} exited {process.returncode}; log at {log}")
        try:
            if httpx.get(f"{url}/livez", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    pytest.fail(f"engine at {url} did not answer /livez in {_START_SECONDS}s; log at {log}")


@contextmanager
def _engines(root: Path, env: dict[str, str], count: int) -> Iterator[list[str]]:
    """*count* engine processes, each up before the next starts, with its own config directory and port."""
    launched: list[tuple[str, subprocess.Popen, Path]] = []
    try:
        for n in range(count):
            home = root / f"engine-{n}"
            (home / "config").mkdir(parents=True)
            port = _free_port()
            log = home / "engine.log"
            own = {**env, "HOME": str(home), "DFE_CONFIG_DIR": str(home / "config")}
            with log.open("wb") as out:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "--factory",
                        "dfe_engine.api.app:create_app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ],
                    cwd=home,
                    env=own,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                )
            launched.append((f"http://127.0.0.1:{port}", process, log))
            _wait_live(*launched[-1])
        yield [url for url, _, _ in launched]
    finally:
        for _, process, _ in launched:
            process.terminate()
        for _, process, _ in launched:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=30)


@dataclass(frozen=True)
class _Pair:
    """Two running engines, the admin password they share, and the secrets settings they run on."""

    a: str
    b: str
    password: str = field(repr=False)
    settings: SecretsSettings


@pytest.fixture(scope="module")
def engines(openbao, tmp_path_factory) -> Iterator[_Pair]:
    mount = openbao.kv_mount()
    role_id, secret_id = _approle(openbao, mount)
    password = random_tokens.token_urlsafe(16)
    root = tmp_path_factory.mktemp("engines")
    # Only what the engine reads, so nothing on the host reaches it.
    env = {
        "PATH": os.environ["PATH"],
        "LANG": "C.UTF-8",
        "DFE_ENV": "test",
        "DFE_AUTH_ENABLED": "true",
        # One account store, as replicas share theirs. The secrets are what this module is about.
        "DFE_AUTH_DIR": str(root / "auth"),
        "DFE_AUTH_LOCAL_ADMIN_PASSWORD": password,
        "DFE_CLICKHOUSE_BOOTSTRAP_TABLES": "false",
        "DFE_SECRETS_PROVIDER": "openbao",
        "DFE_SECRETS_ADDR": openbao.address,
        "DFE_SECRETS_MOUNT": mount,
        "DFE_SECRETS_ROLE": role_id,
        "VAULT_SECRET_ID": secret_id,
    }
    settings = SecretsSettings(provider="openbao", addr=openbao.address, mount=mount, role=role_id)

    with _engines(root, env, count=2) as (a, b):
        # The issued password allows no other write until it is replaced.
        replaced = random_tokens.token_urlsafe(16)
        changed = httpx.post(
            f"{a}/api/v1/auth/accounts/reset-password",
            json={"current_password": password, "new_password": replaced},
            headers={"Authorization": f"Bearer {_signed_in(a, password)}"},
            timeout=30,
        )
        assert changed.status_code == 200, changed.text
        yield _Pair(a=a, b=b, password=replaced, settings=settings)


def _signed_in(url: str, password: str) -> str:
    """An access token for the local admin, from the engine at *url*."""
    signed_in = httpx.post(
        f"{url}/api/v1/auth/login",
        json={"username": "admin", "password": password},
        timeout=10,
    )
    assert signed_in.status_code == 200, signed_in.text
    return signed_in.json()["access_token"]


def test_two_engines_on_one_openbao_mint_one_key_and_read_each_other_s_tokens(openbao, engines):
    keys_a = httpx.get(f"{engines.a}/.well-known/jwks.json", timeout=10).json()["keys"]
    keys_b = httpx.get(f"{engines.b}/.well-known/jwks.json", timeout=10).json()["keys"]
    token = _signed_in(engines.a, engines.password)

    me = httpx.get(
        f"{engines.b}/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}, timeout=10
    )

    assert len(keys_a) == 1
    assert keys_a == keys_b
    assert _versions(openbao, engines.settings.mount, "jwt/signing-key") == 1
    assert me.status_code == 200, me.text
    assert me.json()["user_id"] == "admin"


def test_the_receiver_s_secret_source_names_the_bearer_secret_an_engine_wrote(openbao, engines):
    token = _signed_in(engines.a, engines.password)

    created = httpx.put(
        f"{engines.a}/api/v1/orgs/default/ingest-credentials/agents",
        json={"listeners": ["http"]},
        headers={"Authorization": f"Bearer {token}", "If-None-Match": "*"},
        timeout=30,
    )

    assert created.status_code == 201, created.text
    # vault:<mount>/<path>:<key>, as dfe-receiver resolves it.
    source = bearer_secret_source("http", engines.settings)
    assert source is not None
    named, key = source.removeprefix("vault:").rsplit(":", 1)
    mount, path = named.split("/", 1)
    stored = openbao.call("GET", f"{mount}/data/{path}")["data"]["data"][key]
    assert stored == f"agents:{created.json()['token']}\n"
