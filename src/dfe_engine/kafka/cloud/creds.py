#  Project:      dfe-engine
#  File:         kafka/cloud/creds.py
#  Purpose:      Shared cred-persistence for the managed-Kafka lifecycle
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Persist / forget a minted :class:`KafkaConnection` - ``.env`` + scalo.secrets.

Two halves, per ``docs/MANAGED-KAFKA-LIFECYCLE.md``'s "persist creds (.env +
scalo.secrets)" checkpoint:

- ``.env`` upsert (:func:`upsert_env_file`) - so anything reading ``.env``
  (``dfe kafka client-config``, a shell ``source .env``) picks the new cluster
  up immediately. Uses the SAME key names the ``.tmp/redpanda_lifecycle.py``
  scratch driver already printed for manual paste (this promotes the paste
  into an automatic write - see that script's ``up()``/``status_cmd()``).
- ``scalo.secrets`` (:mod:`dfe_engine.secrets` - ``DfeSecrets``) - the SAME
  seam the engine uses for every OTHER secret it mints (see
  ``dfe_engine.governance.ch.reconciler._hash_for``), so the password also
  lands wherever ``DFE_SECRETS_PROVIDER`` points (file/openbao/...), not only
  a plaintext ``.env``.

This is the seam the WS-C master plan (docs/superpowers/plans/2026-07-12-
kafka-contract-and-confluent-lifecycle.md) calls out to "build ONCE, shared
with the ClickHouse Cloud lifecycle". ``cli/ch_cloud.py`` does not itself mint
or persist a connection today - its ``CloudService`` only starts/stops an
ALREADY-provisioned service (no create/mint step), so there is nothing
literal to import from it. The reusable half is these two primitives
(``upsert_env_file`` + ``dfe_engine.secrets.DfeSecrets``), which are already
provider-agnostic; a future CH Cloud create/mint flow calls the same two,
not a Kafka-specific wrapper.
"""

from __future__ import annotations

import re
from pathlib import Path

from dfe_engine.secrets import DfeSecrets, build_secrets
from dfe_engine.settings import SecretsSettings

from .base import KafkaConnection

_ENV_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")

# provider -> .env key names the lifecycle reads/writes (matches the names
# already used in this repo's .env, previously hand-pasted from the scratch
# driver's printed output).
_ENV_KEYS: dict[str, dict[str, str]] = {
    "redpanda-cloud": {
        "cluster_id": "DFE_REDPANDA_CLUSTER_ID",
        "bootstrap_servers": "DFE_REDPANDA_BOOTSTRAP_SERVERS",
        "http_endpoint": "DFE_REDPANDA_HTTP_ENDPOINT",
        "security_protocol": "DFE_REDPANDA_SECURITY_PROTOCOL",
        "sasl_mechanism": "DFE_REDPANDA_SASL_MECHANISM",
        "username": "DFE_REDPANDA_KAFKA_USERNAME",
        "password": "DFE_REDPANDA_KAFKA_PASSWORD",
    },
}

# scalo.secrets path prefix per provider (mirrors reconciler.py's "ch/..." shape).
_SECRETS_PREFIX: dict[str, str] = {
    "redpanda-cloud": "kafka/redpanda-cloud",
}


def upsert_env_file(pairs: dict[str, str], *, env_path: str | Path) -> None:
    """Idempotently set ``KEY="value"`` lines in a .env file.

    Updates a key in place when a line already declares it; appends any key
    not already present. Every other line is left byte-for-byte untouched -
    this never reformats, reorders, or drops unrelated lines (including
    comments/blank lines), and creates the file if it does not exist yet.
    """
    path = Path(env_path)
    lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(pairs)
    out: list[str] = []
    for line in lines:
        match = _ENV_KEY_RE.match(line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            out.append(f'{key}="{remaining.pop(key)}"')
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f'{key}="{value}"')
    path.write_text("\n".join(out) + "\n")


def _env_pairs(provider: str, conn: KafkaConnection) -> dict[str, str]:
    keys = _ENV_KEYS.get(provider)
    if not keys:
        raise ValueError(f"no .env key mapping for provider {provider!r}")
    pairs = {
        keys["cluster_id"]: conn.cluster_id,
        keys["bootstrap_servers"]: conn.bootstrap_servers,
        keys["security_protocol"]: conn.security_protocol,
        keys["sasl_mechanism"]: conn.sasl_mechanism,
        keys["username"]: conn.username,
        keys["password"]: conn.password,
    }
    endpoint_key = keys.get("http_endpoint")
    if endpoint_key and "http_endpoint" in conn.extra:
        pairs[endpoint_key] = conn.extra["http_endpoint"]
    return pairs


def persist_connection(
    conn: KafkaConnection,
    *,
    provider: str,
    secrets_settings: SecretsSettings,
    env_path: str | Path = ".env",
    secrets: DfeSecrets | None = None,
) -> None:
    """Persist a minted connection: ``.env`` upsert + scalo.secrets (username+password).

    ``secrets`` is injectable (tests pass a fake / a real file-backed store
    over ``tmp_path``); defaults to ``build_secrets(secrets_settings)`` - the
    engine's real mint-secret seam.
    """
    upsert_env_file(_env_pairs(provider, conn), env_path=env_path)
    store = secrets if secrets is not None else build_secrets(secrets_settings)
    prefix = _SECRETS_PREFIX.get(provider, f"kafka/{provider}")
    store.put(f"{prefix}/username", conn.username)
    store.put(f"{prefix}/password", conn.password)


def forget_connection(
    *,
    provider: str,
    secrets_settings: SecretsSettings,
    secrets: DfeSecrets | None = None,
) -> None:
    """Remove the persisted secret half of a torn-down connection.

    Called AFTER the provider has already deleted the data-plane user (mirrors
    ``down``'s teardown-to-empty discipline - creds die first control-plane
    side, this just catches up the local store), so it never races a live
    credential. The ``.env`` half is deliberately left alone: a stale
    bootstrap/user entry after ``down`` is a harmless breadcrumb (the password
    is already revoked control-plane side) - scrubbing it is not required to
    reach $0/empty.
    """
    store = secrets if secrets is not None else build_secrets(secrets_settings)
    prefix = _SECRETS_PREFIX.get(provider, f"kafka/{provider}")
    store.delete(f"{prefix}/username")
    store.delete(f"{prefix}/password")
