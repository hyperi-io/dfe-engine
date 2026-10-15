#  Project:      dfe-engine
#  File:         tests/unit/test_secrets_seam.py
#  Purpose:      The scalo.secrets seam - real file backend, no mocks
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The minted-secret seam against a real scalo.secrets file backend, and a scripted AWS answer.

The scripted manager stands in only where AWS behaves in a way moto does not: a forced
delete that finishes in the background, a refused read and a throttled one.
"""

from typing import cast

import pytest
from scalo.secrets import SecretsManager
from scalo.secrets.exceptions import (
    ProviderError,
    SecretPendingDeletionError,
    SecretPermissionError,
)

from dfe_engine.secrets import (
    FORCED_DELETE_BACKOFF,
    DfeSecrets,
    SecretNameHeldError,
    build_secrets,
)
from dfe_engine.settings import SecretsSettings


class _ScriptedManager:
    """Answers a scalo manager's calls as AWS does while a forced delete is still finishing."""

    def __init__(self, *, pending_creates: int = 0, read_error: Exception | None = None) -> None:
        self.created_tags: list[dict[str, str] | None] = []
        self._pending_creates = pending_creates
        self._read_error = read_error

    def create_sync(self, path: str, value: bytes, tags=None, provider=None) -> None:
        self.created_tags.append(tags)
        if self._pending_creates:
            self._pending_creates -= 1
            raise SecretPendingDeletionError("aws", "CreateSecret", path)

    def get_sync(self, path: str, key=None, provider=None):
        raise self._read_error or SecretPendingDeletionError("aws", "GetSecretValue", path)

    def clear_cache(self) -> None:
        """Hold no cache."""


def _scripted_store(
    manager: _ScriptedManager, *, provider: str = "aws", window: int = 0, sleeps=None
) -> DfeSecrets:
    return DfeSecrets(
        cast("SecretsManager", manager),
        provider=provider,
        root="",
        name_prefix="acme/dfe/test/engine",
        tags={"dfe-run": "r1"},
        recovery_window_days=window,
        backoff=(1.0, 2.0, 4.0),
        sleep=(sleeps if sleeps is not None else []).append,
    )


def test_file_provider_put_get_delete(tmp_path):
    sec = build_secrets(SecretsSettings(provider="file", path=str(tmp_path)))
    sec.put("ch/groups/soc-ro", "hunter2")
    assert sec.get("ch/groups/soc-ro") == "hunter2"
    assert sec.exists("ch/groups/soc-ro") is True
    # upsert (rotation) must overwrite, not raise
    sec.put("ch/groups/soc-ro", "rotated")
    assert sec.get("ch/groups/soc-ro") == "rotated"
    sec.delete("ch/groups/soc-ro")
    assert sec.exists("ch/groups/soc-ro") is False
    # delete is idempotent
    sec.delete("ch/groups/soc-ro")


def test_missing_secret_raises(tmp_path):
    from scalo.secrets.exceptions import SecretNotFoundError

    sec = build_secrets(SecretsSettings(provider="file", path=str(tmp_path)))
    with pytest.raises(SecretNotFoundError):
        sec.get("does/not/exist")


def test_a_read_after_the_store_stops_answering_raises(tmp_path):
    """A store outage shows on the next read, never as the last value it handed out."""
    sec = build_secrets(SecretsSettings(provider="file", path=str(tmp_path)))
    sec.put("oidc/acme/client_secret", "first")
    assert sec.get("oidc/acme/client_secret") == "first"
    stored = tmp_path / "oidc" / "acme" / "client_secret"
    stored.unlink()
    # Reading a directory fails inside the real file provider, as a store outage does.
    stored.mkdir()

    with pytest.raises(ProviderError):
        sec.get("oidc/acme/client_secret")


def test_a_secret_rotated_outside_the_engine_is_read_fresh(tmp_path):
    """ESO or an operator rotates the value behind the engine's back, so no write clears a cache."""
    sec = build_secrets(SecretsSettings(provider="file", path=str(tmp_path)))
    sec.put("oidc/acme/client_secret", "first")
    assert sec.get("oidc/acme/client_secret") == "first"

    (tmp_path / "oidc" / "acme" / "client_secret").write_text("rotated", encoding="utf-8")

    assert sec.get("oidc/acme/client_secret") == "rotated"


# -- a forced delete AWS has not finished -------------------------------------


@pytest.mark.parametrize("write", ["put", "create"])
def test_a_write_waits_out_a_forced_delete_that_finishes(write):
    sleeps: list[float] = []
    manager = _ScriptedManager(pending_creates=2)
    store = _scripted_store(manager, sleeps=sleeps)

    getattr(store, write)("oidc/okta/client_secret", "new")

    assert len(manager.created_tags) == 3
    assert sleeps == [1.0, 2.0]


def test_a_forced_delete_that_never_finishes_gives_up_after_the_backoff():
    sleeps: list[float] = []
    manager = _ScriptedManager(pending_creates=100)
    store = _scripted_store(manager, sleeps=sleeps)

    with pytest.raises(SecretNameHeldError, match="Restore the secret, or wait out") as held:
        store.put("ch/groups/soc", "new")

    assert "acme/dfe/test/engine/ch/groups/soc" in str(held.value)
    assert len(manager.created_tags) == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_a_recovery_window_is_refused_without_waiting():
    sleeps: list[float] = []
    manager = _ScriptedManager(pending_creates=1)
    store = _scripted_store(manager, window=7, sleeps=sleeps)

    with pytest.raises(SecretNameHeldError, match="DFE_SECRETS_RECOVERY_WINDOW_DAYS=7"):
        store.put("ch/groups/soc", "new")

    assert len(manager.created_tags) == 1
    assert sleeps == []


def test_the_default_backoff_waits_about_30_seconds():
    assert sum(FORCED_DELETE_BACKOFF) == 30.0


def test_a_name_still_being_force_deleted_reads_as_gone():
    """The value is going for good, so the next write mints and waits for the name."""
    assert _scripted_store(_ScriptedManager()).get_or_none("ch/groups/soc") is None


def test_a_name_held_by_a_recovery_window_is_not_read_as_gone():
    with pytest.raises(SecretNameHeldError):
        _scripted_store(_ScriptedManager(), window=7).get_or_none("ch/groups/soc")


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            SecretPermissionError("aws", "GetSecretValue", "acme/x", "check IAM"), id="denied"
        ),
        pytest.param(
            ProviderError("aws", "GetSecretValue acme/x failed: ThrottlingException"),
            id="throttled",
        ),
    ],
)
def test_a_refused_or_throttled_read_is_not_read_as_absent(error):
    """An absent secret gets minted, so a read that failed must never look like one."""
    store = _scripted_store(_ScriptedManager(read_error=error))

    with pytest.raises(ProviderError) as raised:
        store.get_or_none("ch/groups/soc")

    assert raised.value is error


@pytest.mark.parametrize(("provider", "tags"), [("aws", {"dfe-run": "r1"}), ("openbao", None)])
def test_tags_reach_aws_only(provider, tags):
    """OpenBao stores tags after the value, so a failed tag write would follow a stored secret."""
    manager = _ScriptedManager()

    _scripted_store(manager, provider=provider).create("ch/groups/soc", "pw")

    assert manager.created_tags == [tags]
