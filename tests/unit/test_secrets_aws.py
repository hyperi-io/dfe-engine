#  Project:      dfe-engine
#  File:         tests/unit/test_secrets_aws.py
#  Purpose:      Minted secrets in AWS Secrets Manager, against moto's in-process AWS
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Minted secrets stored in AWS Secrets Manager, read back the way the engine reads them.

moto runs AWS in process, so nothing here leaves the host or needs an account.
"""

import boto3
import pytest
from moto import mock_aws
from scalo.logger import logger
from scalo.secrets import SecretsManager
from scalo.secrets.exceptions import ProviderError, SecretAlreadyExistsError

from dfe_engine.api.deps import jwt_authority_for
from dfe_engine.auth.jwt_authority import JwtAuthority
from dfe_engine.governance.ch.reconciler import ChRbacReconciler
from dfe_engine.secrets import SecretNameHeldError, build_secrets, warn_on_aws_managed_key
from dfe_engine.settings import DFESettings, SecretsSettings
from tests.support.racing_stores import RacingSecrets

REGION = "ap-southeast-2"
PREFIX = "acme/dfe/test/engine"
TAGS = {"dfe-run": "r1", "owner": "acme"}
ISS = "https://dfe.test/api"


@pytest.fixture
def aws(monkeypatch, tmp_path):
    """An in-process AWS with throwaway credentials, and none of the host's profile or region."""
    for name in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "aws-credentials"))
    with mock_aws():
        yield


def _settings(**fields: object) -> SecretsSettings:
    base = {"provider": "aws", "region": REGION, "name_prefix": PREFIX, "tags": TAGS}
    return SecretsSettings.model_validate(base | fields)


def _client(region: str = REGION):
    return boto3.client("secretsmanager", region_name=region)


def _described(path: str) -> dict:
    return _client().describe_secret(SecretId=f"{PREFIX}/{path}")


# -- the store ----------------------------------------------------------------


def test_a_secret_round_trips_under_the_prefix(aws):
    store = build_secrets(_settings())

    store.put("ch/orgs/acme", "first")
    assert store.get("ch/orgs/acme") == "first"
    assert store.get_or_none("ch/orgs/acme") == "first"
    assert store.exists("ch/orgs/acme") is True
    store.put("ch/orgs/acme", "rotated")
    assert store.get("ch/orgs/acme") == "rotated"
    assert [s["Name"] for s in _client().list_secrets()["SecretList"]] == [f"{PREFIX}/ch/orgs/acme"]

    store.delete("ch/orgs/acme")
    assert store.get_or_none("ch/orgs/acme") is None
    assert store.exists("ch/orgs/acme") is False
    store.delete("ch/orgs/acme")

    # Window 0 deletes with no recovery window, so a re-created org gets the name back.
    store.put("ch/orgs/acme", "again")
    assert store.get("ch/orgs/acme") == "again"


def test_a_secret_lands_in_the_configured_region_only(aws):
    build_secrets(_settings()).put("jwt/signing-key", "pem")

    assert _described("jwt/signing-key")["Name"] == f"{PREFIX}/jwt/signing-key"
    assert _client("us-east-1").list_secrets()["SecretList"] == []


def test_two_prefixes_in_one_account_never_share_a_name(aws):
    build_secrets(_settings(name_prefix="acme/dfe/a/engine")).put("jwt/signing-key", "a")
    build_secrets(_settings(name_prefix="acme/dfe/b/engine")).put("jwt/signing-key", "b")

    assert build_secrets(_settings(name_prefix="acme/dfe/a/engine")).get("jwt/signing-key") == "a"
    assert build_secrets(_settings(name_prefix="acme/dfe/b/engine")).get("jwt/signing-key") == "b"


def test_a_created_secret_carries_the_kms_key_and_the_tags(aws):
    key_arn = boto3.client("kms", region_name=REGION).create_key()["KeyMetadata"]["Arn"]
    store = build_secrets(_settings(kms_key_id=key_arn))

    store.put("ch/service/query_reader", "pw")
    store.put("ch/service/query_reader", "rotated")

    described = _described("ch/service/query_reader")
    assert described["KmsKeyId"] == key_arn
    assert {t["Key"]: t["Value"] for t in described["Tags"]} == TAGS


def test_without_a_kms_key_the_secret_names_none(aws):
    build_secrets(_settings()).put("ch/orgs/acme", "pw")

    assert "KmsKeyId" not in _described("ch/orgs/acme")


def test_create_leaves_a_stored_secret_as_it_is(aws):
    store = build_secrets(_settings())
    store.create("jwt/signing-key", "first")

    with pytest.raises(SecretAlreadyExistsError):
        store.create("jwt/signing-key", "second")

    assert store.get("jwt/signing-key") == "first"


def test_a_name_inside_a_recovery_window_is_refused_naming_the_setting(aws):
    store = build_secrets(_settings(recovery_window_days=7))
    store.put("ch/groups/soc", "pw")

    store.delete("ch/groups/soc")

    assert "DeletedDate" in _described("ch/groups/soc")
    with pytest.raises(SecretNameHeldError, match="DFE_SECRETS_RECOVERY_WINDOW_DAYS=7") as held:
        store.put("ch/groups/soc", "new")
    assert isinstance(held.value, ProviderError)
    with pytest.raises(SecretNameHeldError):
        store.get_or_none("ch/groups/soc")
    # A name already scheduled for deletion is already gone as far as a delete goes.
    store.delete("ch/groups/soc")


def test_a_reconcile_recreating_a_group_inside_the_window_fails_naming_the_setting(aws):
    store = build_secrets(_settings(recovery_window_days=7))
    reconciler = ChRbacReconciler(admin_client=None, secrets_store=store, database="dfe")
    reconciler._hash_for("ch/groups/soc")
    store.delete("ch/groups/soc")

    with pytest.raises(SecretNameHeldError, match="DFE_SECRETS_RECOVERY_WINDOW_DAYS"):
        reconciler._hash_for("ch/groups/soc")


# -- the JWT signing key --------------------------------------------------------


def test_the_jwt_authority_mints_into_the_store_and_reloads_from_it(aws):
    first = JwtAuthority(build_secrets(_settings()), issuer=ISS)

    stored = _client().get_secret_value(SecretId=f"{PREFIX}/jwt/signing-key")["SecretString"]
    assert stored.startswith("-----BEGIN PRIVATE KEY-----")
    second = JwtAuthority(build_secrets(_settings()), issuer=ISS)
    assert second.kid == first.kid
    assert second.verify(first.sign({"sub": "alice"}))["sub"] == "alice"


def test_a_replica_that_loses_the_mint_race_signs_with_the_stored_key(aws):
    first = JwtAuthority(build_secrets(_settings()), issuer=ISS)
    manager = SecretsManager.from_config({"aws": {"region": REGION}, "cache": {"enabled": False}})
    racing = RacingSecrets(
        manager, provider="aws", root="", name_prefix=PREFIX, blind_to="jwt/signing-key"
    )

    second = JwtAuthority(racing, issuer=ISS)

    assert second.kid == first.kid
    assert first.verify(second.sign({"sub": "bob"}))["sub"] == "bob"
    assert JwtAuthority(build_secrets(_settings()), issuer=ISS).kid == first.kid


def test_two_prefixes_get_two_signing_keys_from_the_shared_authority_cache(aws):
    a = jwt_authority_for(
        DFESettings(env="test", secrets=_settings(name_prefix="acme/dfe/cache-a/engine"))
    )
    b = jwt_authority_for(
        DFESettings(env="test", secrets=_settings(name_prefix="acme/dfe/cache-b/engine"))
    )

    assert a is not b
    assert a.kid != b.kid


# -- startup ------------------------------------------------------------------


def test_aws_without_a_kms_key_warns_once_naming_the_setting():
    lines: list[tuple[str, str]] = []
    handler = logger.add(
        lambda message: lines.append((message.record["level"].name, message.record["message"])),
        level="DEBUG",
        format="{message}",
    )
    try:
        warn_on_aws_managed_key(_settings())
        warn_on_aws_managed_key(_settings(kms_key_id="alias/acme-dfe"))
        warn_on_aws_managed_key(SecretsSettings())
    finally:
        logger.remove(handler)

    assert len(lines) == 1
    level, message = lines[0]
    assert level == "WARNING"
    assert "DFE_SECRETS_KMS_KEY_ID" in message
    assert "AWS managed key" in message
