#  Project:      dfe-engine
#  File:         tests/unit/test_settings_secrets.py
#  Purpose:      SecretsSettings - the scalo.secrets backend-by-config seam
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The secrets seam is chosen by config, never hardcoded (see docs/deployment/backing-services.md)."""

import pytest
from pydantic import ValidationError

from dfe_engine.settings import SecretsSettings, load_settings, reset_settings

_AWS = {"provider": "aws", "region": "ap-southeast-2", "prefix": "acme/dfe/test/engine"}

_AWS_ENV = {
    "DFE_SECRETS_PROVIDER": "aws",
    "DFE_SECRETS_REGION": "ap-southeast-2",
    "DFE_SECRETS_PREFIX": "acme/dfe/test/engine",
    "DFE_SECRETS_KMS_KEY_ID": "alias/acme-dfe",
    "DFE_SECRETS_RECOVERY_WINDOW_DAYS": "7",
    "DFE_SECRETS_TAGS": '{"dfe-run": "r1", "owner": "acme"}',
}


@pytest.fixture
def fresh_settings():
    """Load settings from this test's environment, and leave none cached for the next test."""
    reset_settings()
    yield load_settings
    reset_settings()


def test_secrets_defaults_to_file(monkeypatch, fresh_settings):
    for k in ("DFE_SECRETS_PROVIDER", "DFE_SECRETS_PATH", "DFE_SECRETS_ADDR"):
        monkeypatch.delenv(k, raising=False)
    s = fresh_settings()
    assert s.secrets.provider == "file"
    assert s.secrets.recovery_window_days == 0
    assert s.secrets.tags == {}


def test_secrets_provider_from_env(monkeypatch, fresh_settings):
    monkeypatch.setenv("DFE_SECRETS_PROVIDER", "openbao")
    monkeypatch.setenv("DFE_SECRETS_ADDR", "https://bao:8200")
    s = fresh_settings()
    assert s.secrets.provider == "openbao"
    assert s.secrets.addr == "https://bao:8200"


def test_aws_settings_from_env(monkeypatch, fresh_settings):
    for name, value in _AWS_ENV.items():
        monkeypatch.setenv(name, value)

    s = fresh_settings().secrets

    assert s.provider == "aws"
    assert s.region == "ap-southeast-2"
    assert s.prefix == "acme/dfe/test/engine"
    assert s.kms_key_id == "alias/acme-dfe"
    assert s.recovery_window_days == 7
    assert s.tags == {"dfe-run": "r1", "owner": "acme"}


def test_malformed_tags_json_refuses_to_load(monkeypatch, fresh_settings):
    monkeypatch.setenv("DFE_SECRETS_TAGS", "{dfe-run: r1")
    with pytest.raises(ValueError, match="DFE_SECRETS_TAGS is not valid JSON"):
        fresh_settings()


@pytest.mark.parametrize("tags", ['["dfe-run"]', '{"dfe-run": 1}'])
def test_tags_that_are_not_a_string_map_refuse_to_load(monkeypatch, fresh_settings, tags):
    monkeypatch.setenv("DFE_SECRETS_TAGS", tags)
    with pytest.raises(ValidationError):
        fresh_settings()


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        pytest.param({"region": ""}, "needs DFE_SECRETS_REGION", id="no-region"),
        pytest.param({"prefix": ""}, "needs DFE_SECRETS_PREFIX", id="no-prefix"),
        pytest.param({"prefix": "/acme/dfe"}, "must not start or end", id="leading-slash"),
        pytest.param({"prefix": "acme/dfe/"}, "must not start or end", id="trailing-slash"),
        pytest.param({"prefix": "AmazonMSK_acme"}, "'AmazonMSK_'", id="msk-prefix"),
        pytest.param({"recovery_window_days": -1}, "0 .* or 7-30, got -1", id="window-negative"),
        pytest.param({"recovery_window_days": 1}, "0 .* or 7-30, got 1", id="window-1"),
        pytest.param({"recovery_window_days": 6}, "0 .* or 7-30, got 6", id="window-6"),
        pytest.param({"recovery_window_days": 31}, "0 .* or 7-30, got 31", id="window-31"),
    ],
)
def test_aws_refuses_a_setting_it_cannot_store_under(fields, message):
    with pytest.raises(ValidationError, match=message):
        SecretsSettings.model_validate(_AWS | fields)


@pytest.mark.parametrize("window", [0, 7, 30])
def test_aws_accepts_each_window_aws_allows(window):
    settings = SecretsSettings.model_validate(_AWS | {"recovery_window_days": window})
    assert settings.recovery_window_days == window


@pytest.mark.parametrize("provider", ["gcp", "azure"])
def test_a_provider_whose_names_allow_no_slash_is_refused(provider):
    with pytest.raises(ValidationError, match=f"DFE_SECRETS_PROVIDER={provider} is not supported"):
        SecretsSettings(provider=provider)


def test_the_aws_fields_bind_only_the_aws_provider():
    s = SecretsSettings(provider="file", prefix="/not-checked/", recovery_window_days=3)
    assert s.provider == "file"
