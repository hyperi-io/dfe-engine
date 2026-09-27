#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_api_keys.py
#  Purpose:      Tests for APIKeyStore — YAML-backed API key management
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from dfe_engine.auth.api_keys import APIKey, APIKeyStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> APIKeyStore:
    return APIKeyStore(tmp_path / "api_keys")


def _set_expiry(store: APIKeyStore, name: str, expires_at: str | None) -> None:
    """Force a key's stored expiry, bypassing create()'s future-only rule."""
    import dfe_engine.yaml_utils as yu

    key_file = store._keys_dir / f"{name}.yaml"
    data = yu.yaml_load(key_file)
    data["expires_at"] = expires_at
    yu.yaml_dump(data, key_file)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestCreate:
    def test_returns_metadata_and_full_key(self, store: APIKeyStore) -> None:
        key_meta, full_key = store.create("ci-pipeline")
        assert isinstance(key_meta, APIKey)
        assert isinstance(full_key, str)

    def test_create_rejects_unsafe_name(self, store: APIKeyStore) -> None:
        # Name becomes the {name}.yaml filename stem - reject traversal /
        # separators / a trailing newline.
        for bad in ("../evil", "a/b", "..", ".hidden", "with space", "bad\n", ""):
            with pytest.raises(ValueError, match="Invalid API key name"):
                store.create(bad)

    def test_full_key_has_correct_prefix(self, store: APIKeyStore) -> None:
        _meta, full_key = store.create("ci-pipeline")
        assert full_key.startswith("dfe_ak_")

    def test_full_key_format_four_parts(self, store: APIKeyStore) -> None:
        _meta, full_key = store.create("ci-pipeline")
        parts = full_key.split("_", maxsplit=3)
        assert len(parts) == 4
        assert parts[0] == "dfe"
        assert parts[1] == "ak"

    def test_short_token_is_8_hex_chars(self, store: APIKeyStore) -> None:
        key_meta, full_key = store.create("ci-pipeline")
        parts = full_key.split("_", maxsplit=3)
        short = parts[2]
        assert len(short) == 8
        assert all(c in "0123456789abcdef" for c in short)
        assert key_meta.short_token == short

    def test_long_token_is_32_plus_hex_chars(self, store: APIKeyStore) -> None:
        _meta, full_key = store.create("ci-pipeline")
        parts = full_key.split("_", maxsplit=3)
        long_token = parts[3]
        assert len(long_token) >= 32
        assert all(c in "0123456789abcdef" for c in long_token)

    def test_hash_stored_with_sha384_prefix(self, store: APIKeyStore) -> None:
        key_meta, _full_key = store.create("ci-pipeline")
        assert key_meta.key_hash.startswith("sha384:")

    def test_hash_matches_long_token(self, store: APIKeyStore) -> None:
        key_meta, full_key = store.create("ci-pipeline")
        long_token = full_key.split("_", maxsplit=3)[3]
        expected_hash = hashlib.sha384(long_token.encode()).hexdigest()
        stored_hash = key_meta.key_hash.removeprefix("sha384:")
        assert stored_hash == expected_hash

    def test_key_written_to_disk(self, store: APIKeyStore) -> None:
        store.create("ci-pipeline")
        assert (store._keys_dir / "ci-pipeline.yaml").exists()

    def test_groups_stored(self, store: APIKeyStore) -> None:
        key_meta, _full_key = store.create("ci-pipeline", groups=["infra_admin"])
        assert key_meta.groups == ["infra_admin"]

    def test_description_stored(self, store: APIKeyStore) -> None:
        key_meta, _full_key = store.create("ci-pipeline", description="Used by GitHub Actions")
        assert key_meta.description == "Used by GitHub Actions"

    def test_enabled_true_by_default(self, store: APIKeyStore) -> None:
        key_meta, _full_key = store.create("ci-pipeline")
        assert key_meta.enabled is True

    def test_created_at_is_populated(self, store: APIKeyStore) -> None:
        key_meta, _full_key = store.create("ci-pipeline")
        assert key_meta.created_at != ""

    def test_name_matches_requested(self, store: APIKeyStore) -> None:
        key_meta, _full_key = store.create("my-service")
        assert key_meta.name == "my-service"

    def test_duplicate_name_raises_value_error(self, store: APIKeyStore) -> None:
        store.create("ci-pipeline")
        with pytest.raises(ValueError, match="already exists"):
            store.create("ci-pipeline")

    def test_expires_at_none_by_default(self, store: APIKeyStore) -> None:
        key_meta, _full_key = store.create("ci-pipeline")
        assert key_meta.expires_at is None

    def test_expires_at_stored_normalised_to_utc(self, store: APIKeyStore) -> None:
        future = datetime.now(UTC) + timedelta(days=30)
        aest = future.astimezone(timezone(timedelta(hours=10)))
        key_meta, _full_key = store.create("ci-pipeline", expires_at=aest.isoformat())
        assert key_meta.expires_at is not None
        stored = datetime.fromisoformat(key_meta.expires_at)
        assert stored.utcoffset() == timedelta(0)
        assert stored == future

    def test_expires_at_accepts_trailing_z(self, store: APIKeyStore) -> None:
        future = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
        key_meta, _full_key = store.create(
            "ci-pipeline", expires_at=future.isoformat().replace("+00:00", "Z")
        )
        assert key_meta.expires_at is not None
        assert datetime.fromisoformat(key_meta.expires_at) == future

    def test_naive_expires_at_read_as_utc(self, store: APIKeyStore) -> None:
        future = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
        key_meta, _full_key = store.create(
            "ci-pipeline", expires_at=future.replace(tzinfo=None).isoformat()
        )
        assert key_meta.expires_at is not None
        assert datetime.fromisoformat(key_meta.expires_at) == future

    def test_expires_at_survives_round_trip(self, store: APIKeyStore) -> None:
        future = datetime.now(UTC) + timedelta(days=7)
        store.create("ci-pipeline", expires_at=future.isoformat())
        reloaded = store.get("ci-pipeline")
        assert reloaded is not None
        assert reloaded.expires_at is not None
        assert datetime.fromisoformat(reloaded.expires_at) == future

    def test_past_expires_at_rejected(self, store: APIKeyStore) -> None:
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        with pytest.raises(ValueError, match="in the past"):
            store.create("ci-pipeline", expires_at=past)

    def test_rejected_expiry_writes_no_key_file(self, store: APIKeyStore) -> None:
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        with pytest.raises(ValueError):
            store.create("ci-pipeline", expires_at=past)
        assert store.get("ci-pipeline") is None

    def test_unparseable_expires_at_rejected(self, store: APIKeyStore) -> None:
        for bad in ("next tuesday", "", "2026-13-45", "1789562412"):
            with pytest.raises(ValueError, match="Invalid expires_at"):
                store.create("ci-pipeline", expires_at=bad)


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


class TestIsExpired:
    def test_no_expiry_never_expires(self) -> None:
        key = APIKey(name="k", short_token="deadbeef", key_hash="sha384:x")
        assert key.is_expired() is False

    def test_future_expiry_not_expired(self) -> None:
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        key = APIKey(name="k", short_token="deadbeef", key_hash="sha384:x", expires_at=future)
        assert key.is_expired() is False

    def test_past_expiry_is_expired(self) -> None:
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        key = APIKey(name="k", short_token="deadbeef", key_hash="sha384:x", expires_at=past)
        assert key.is_expired() is True

    def test_expiry_boundary_is_exclusive(self) -> None:
        now = datetime.now(UTC)
        key = APIKey(
            name="k", short_token="deadbeef", key_hash="sha384:x", expires_at=now.isoformat()
        )
        assert key.is_expired(now=now) is True

    def test_unparseable_expiry_counts_as_expired(self) -> None:
        key = APIKey(name="k", short_token="deadbeef", key_hash="sha384:x", expires_at="not-a-date")
        assert key.is_expired() is True

    def test_naive_stored_expiry_compared_as_utc(self) -> None:
        past = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
        key = APIKey(name="k", short_token="deadbeef", key_hash="sha384:x", expires_at=past)
        assert key.is_expired() is True


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


class TestGet:
    def test_get_returns_metadata(self, store: APIKeyStore) -> None:
        store.create("ci-pipeline")
        key_meta = store.get("ci-pipeline")
        assert isinstance(key_meta, APIKey)
        assert key_meta.name == "ci-pipeline"

    def test_get_nonexistent_returns_none(self, store: APIKeyStore) -> None:
        result = store.get("nonexistent")
        assert result is None

    def test_get_does_not_expose_full_key(self, store: APIKeyStore) -> None:
        _meta, full_key = store.create("ci-pipeline")
        key_meta = store.get("ci-pipeline")
        assert key_meta is not None
        # The metadata should have a hash, not the raw token
        long_token = full_key.split("_", maxsplit=3)[3]
        assert long_token not in key_meta.key_hash

    def test_a_name_that_is_a_path_reads_no_file(self, store: APIKeyStore, tmp_path: Path) -> None:
        """An ``apikey:<name>`` session subject reaches this lookup, so it never resolves a path."""
        APIKeyStore(tmp_path / "elsewhere").create("outside", groups=["dfe-admins"])

        assert store.get("../elsewhere/outside") is None


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestList:
    def test_list_empty_store(self, store: APIKeyStore) -> None:
        assert store.list() == []

    def test_list_returns_all_keys(self, store: APIKeyStore) -> None:
        store.create("key-a")
        store.create("key-b")
        store.create("key-c")
        keys = store.list()
        assert len(keys) == 3

    def test_list_sorted_by_name(self, store: APIKeyStore) -> None:
        store.create("zzz-key")
        store.create("aaa-key")
        store.create("mmm-key")
        keys = store.list()
        names = [k.name for k in keys]
        assert names == sorted(names)

    def test_list_metadata_has_short_token(self, store: APIKeyStore) -> None:
        _meta, full_key = store.create("ci-pipeline")
        short_token = full_key.split("_", maxsplit=3)[2]
        keys = store.list()
        assert keys[0].short_token == short_token

    def test_list_metadata_has_hash(self, store: APIKeyStore) -> None:
        store.create("ci-pipeline")
        keys = store.list()
        assert keys[0].key_hash.startswith("sha384:")

    def test_list_does_not_expose_full_keys(self, store: APIKeyStore) -> None:
        _meta, full_key = store.create("ci-pipeline")
        long_token = full_key.split("_", maxsplit=3)[3]
        keys = store.list()
        for k in keys:
            assert long_token not in k.key_hash


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


class TestVerify:
    def test_valid_key_returns_metadata(self, store: APIKeyStore) -> None:
        store.create("ci-pipeline")
        _meta2, full_key2 = store.create("terraform")
        result = store.verify(full_key2)
        assert result is not None
        assert result.name == "terraform"

    def test_valid_key_correct_groups(self, store: APIKeyStore) -> None:
        _meta, full_key = store.create("ci-pipeline", groups=["infra_admin", "data_analyst"])
        result = store.verify(full_key)
        assert result is not None
        assert result.groups == ["infra_admin", "data_analyst"]

    def test_invalid_long_token_returns_none(self, store: APIKeyStore) -> None:
        _meta, full_key = store.create("ci-pipeline")
        parts = full_key.split("_", maxsplit=3)
        # Replace long token with wrong value
        bad_key = f"dfe_ak_{parts[2]}_{'f' * 32}"
        result = store.verify(bad_key)
        assert result is None

    def test_unknown_short_token_returns_none(self, store: APIKeyStore) -> None:
        store.create("ci-pipeline")
        bad_key = "dfe_ak_00000000_" + "a" * 32
        result = store.verify(bad_key)
        assert result is None

    def test_wrong_prefix_returns_none(self, store: APIKeyStore) -> None:
        _meta, full_key = store.create("ci-pipeline")
        bad_key = full_key.replace("dfe_ak_", "bad_xx_", 1)
        result = store.verify(bad_key)
        assert result is None

    def test_malformed_key_returns_none(self, store: APIKeyStore) -> None:
        result = store.verify("not-a-valid-key")
        assert result is None

    def test_empty_key_returns_none(self, store: APIKeyStore) -> None:
        result = store.verify("")
        assert result is None

    def test_disabled_key_returns_none(self, store: APIKeyStore) -> None:
        key_meta, full_key = store.create("ci-pipeline")
        # Disable by overwriting the YAML
        import dfe_engine.yaml_utils as yu

        key_file = store._keys_dir / "ci-pipeline.yaml"
        data = yu.yaml_load(key_file)
        data["enabled"] = False
        yu.yaml_dump(data, key_file)
        result = store.verify(full_key)
        assert result is None

    def test_unexpired_key_verifies(self, store: APIKeyStore) -> None:
        future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        _meta, full_key = store.create("ci-pipeline", expires_at=future)
        assert store.verify(full_key) is not None

    def test_expired_key_returns_none(self, store: APIKeyStore) -> None:
        _meta, full_key = store.create("ci-pipeline")
        _set_expiry(store, "ci-pipeline", (datetime.now(UTC) - timedelta(seconds=1)).isoformat())
        assert store.verify(full_key) is None

    def test_expired_key_still_listed(self, store: APIKeyStore) -> None:
        """Expiry blocks auth but leaves the key visible until it is revoked."""
        store.create("ci-pipeline")
        _set_expiry(store, "ci-pipeline", (datetime.now(UTC) - timedelta(days=1)).isoformat())
        assert [k.name for k in store.list()] == ["ci-pipeline"]

    def test_corrupt_stored_expiry_denies(self, store: APIKeyStore) -> None:
        _meta, full_key = store.create("ci-pipeline")
        _set_expiry(store, "ci-pipeline", "whenever")
        assert store.verify(full_key) is None

    def test_verify_uses_timing_safe_comparison(self, store: APIKeyStore) -> None:
        """Verify returns None for wrong long token regardless of short-token match."""
        _meta, full_key = store.create("ci-pipeline")
        parts = full_key.split("_", maxsplit=3)
        # Same short token, wrong long token
        crafted = f"dfe_ak_{parts[2]}_{'0' * len(parts[3])}"
        result = store.verify(crafted)
        assert result is None


# ---------------------------------------------------------------------------
# Verify (detailed reasons)
# ---------------------------------------------------------------------------


class TestVerifyDetailed:
    def test_valid_key_reason_ok(self, store: APIKeyStore) -> None:
        _meta, full_key = store.create("ci-pipeline")
        key_meta, reason = store.verify_detailed(full_key)
        assert key_meta is not None
        assert reason == "ok"

    def test_malformed_key_reason(self, store: APIKeyStore) -> None:
        assert store.verify_detailed("not-a-valid-key") == (None, "malformed_key")

    def test_unknown_short_token_reason(self, store: APIKeyStore) -> None:
        store.create("ci-pipeline")
        assert store.verify_detailed("dfe_ak_00000000_" + "a" * 32) == (None, "unknown_key")

    def test_wrong_long_token_reason(self, store: APIKeyStore) -> None:
        meta, _full_key = store.create("ci-pipeline")
        bad = f"dfe_ak_{meta.short_token}_{'f' * 32}"
        assert store.verify_detailed(bad) == (None, "invalid_key")

    def test_expired_key_reason(self, store: APIKeyStore) -> None:
        _meta, full_key = store.create("ci-pipeline")
        _set_expiry(store, "ci-pipeline", (datetime.now(UTC) - timedelta(days=1)).isoformat())
        assert store.verify_detailed(full_key) == (None, "expired_key")

    def test_disabled_key_reason(self, store: APIKeyStore) -> None:
        import dfe_engine.yaml_utils as yu

        _meta, full_key = store.create("ci-pipeline")
        key_file = store._keys_dir / "ci-pipeline.yaml"
        data = yu.yaml_load(key_file)
        data["enabled"] = False
        yu.yaml_dump(data, key_file)
        assert store.verify_detailed(full_key) == (None, "disabled_key")

    def test_status_reasons_need_proof_of_possession(self, store: APIKeyStore) -> None:
        """A wrong long token must not reveal that the key is expired/disabled."""
        meta, _full_key = store.create("ci-pipeline")
        _set_expiry(store, "ci-pipeline", (datetime.now(UTC) - timedelta(days=1)).isoformat())
        bad = f"dfe_ak_{meta.short_token}_{'f' * 32}"
        assert store.verify_detailed(bad) == (None, "invalid_key")


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


class TestRevoke:
    def test_revoke_removes_key_file(self, store: APIKeyStore) -> None:
        key_meta, _full_key = store.create("ci-pipeline")
        store.revoke(key_meta.short_token)
        assert store.get("ci-pipeline") is None

    def test_revoke_key_no_longer_verifiable(self, store: APIKeyStore) -> None:
        key_meta, full_key = store.create("ci-pipeline")
        store.revoke(key_meta.short_token)
        assert store.verify(full_key) is None

    def test_revoke_nonexistent_raises_key_error(self, store: APIKeyStore) -> None:
        with pytest.raises(KeyError):
            store.revoke("00000000")

    def test_revoke_does_not_affect_other_keys(self, store: APIKeyStore) -> None:
        meta_a, _full_a = store.create("key-a")
        _meta_b, full_b = store.create("key-b")
        store.revoke(meta_a.short_token)
        assert store.verify(full_b) is not None

    def test_revoke_by_short_token(self, store: APIKeyStore) -> None:
        key_meta, _full_key = store.create("ci-pipeline")
        short = key_meta.short_token
        store.revoke(short)
        keys = store.list()
        assert all(k.short_token != short for k in keys)


# ---------------------------------------------------------------------------
# Key shown once invariant
# ---------------------------------------------------------------------------


class TestKeyShownOnce:
    def test_full_key_only_from_create(self, store: APIKeyStore) -> None:
        """The full key must not be reconstructable from metadata."""
        key_meta, full_key = store.create("ci-pipeline")
        long_token = full_key.split("_", maxsplit=3)[3]

        # get() returns metadata only
        retrieved = store.get("ci-pipeline")
        assert retrieved is not None
        assert long_token not in str(retrieved.model_dump())

    def test_list_does_not_expose_raw_tokens(self, store: APIKeyStore) -> None:
        _meta, full_key = store.create("ci-pipeline")
        long_token = full_key.split("_", maxsplit=3)[3]

        for k in store.list():
            assert long_token not in str(k.model_dump())
