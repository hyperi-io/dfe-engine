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

from dfe_engine.secrets import build_secrets
from dfe_engine.settings import (
    SecretsSettings,
    backing_from_environment,
    load_clickhouse_settings,
    load_settings,
    reset_settings,
)

_AWS = {"provider": "aws", "region": "ap-southeast-2", "name_prefix": "acme/dfe/test/engine"}

_AWS_ENV = {
    "DFE_SECRETS_PROVIDER": "aws",
    "DFE_SECRETS_REGION": "ap-southeast-2",
    "DFE_SECRETS_NAME_PREFIX": "acme/dfe/test/engine",
    "DFE_SECRETS_KMS_KEY_ID": "alias/acme-dfe",
    "DFE_SECRETS_RECOVERY_WINDOW_DAYS": "7",
    "DFE_SECRETS_TAGS": '{"dfe-run": "r1", "owner": "acme"}',
}

# AWS's 512-character name limit less '/' and the longest bounded engine path (161).
_LONGEST_PREFIX = 512 - 1 - 161


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
    assert s.name_prefix == "acme/dfe/test/engine"
    assert s.kms_key_id == "alias/acme-dfe"
    assert s.recovery_window_days == 7
    assert s.tags == {"dfe-run": "r1", "owner": "acme"}


def test_the_bootstrap_prefix_name_is_not_read_as_the_engines(monkeypatch, fresh_settings):
    """bootstrap.sh's DFE_SECRETS_PREFIX names the ESO store root, never the engine's names."""
    monkeypatch.setenv("DFE_SECRETS_PREFIX", "acme-secrets")
    assert fresh_settings().secrets.name_prefix == ""


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
    ("name", "value"),
    [("DFE_SECRETS_TAGS", "{dfe-run: r1"), ("DFE_SECRETS_RECOVERY_WINDOW_DAYS", "seven")],
)
def test_a_bad_secrets_value_leaves_the_narrow_loads_working(monkeypatch, name, value):
    """dfe-schema reads only ClickHouse settings, so a secrets typo must not stop it."""
    monkeypatch.setenv(name, value)

    assert load_clickhouse_settings().host
    backing_from_environment()


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        pytest.param({"region": ""}, "needs DFE_SECRETS_REGION", id="no-region"),
        pytest.param({"name_prefix": ""}, "needs DFE_SECRETS_NAME_PREFIX", id="no-prefix"),
        pytest.param({"name_prefix": "/acme/dfe"}, "must not start or end", id="leading-slash"),
        pytest.param({"name_prefix": "acme/dfe/"}, "must not start or end", id="trailing-slash"),
        pytest.param({"name_prefix": "AmazonMSK_acme"}, "'AmazonMSK_'", id="msk-prefix"),
        pytest.param({"name_prefix": " acme/dfe"}, "only ASCII letters", id="leading-space"),
        pytest.param({"name_prefix": "acme dfe"}, "only ASCII letters", id="inner-space"),
        pytest.param({"name_prefix": "acme/dfe*"}, "only ASCII letters", id="wildcard"),
        pytest.param(
            {"name_prefix": "acme/d" + chr(0xE9) + "fe"}, "only ASCII letters", id="non-ascii"
        ),
        pytest.param({"name_prefix": "a" * 600}, "is 600 characters", id="600-chars"),
        pytest.param(
            {"name_prefix": "a" * (_LONGEST_PREFIX + 1)},
            f"at most {_LONGEST_PREFIX}",
            id="one-over",
        ),
        pytest.param({"recovery_window_days": -1}, "0 .* or 7-30, got -1", id="window-negative"),
        pytest.param({"recovery_window_days": 1}, "0 .* or 7-30, got 1", id="window-1"),
        pytest.param({"recovery_window_days": 6}, "0 .* or 7-30, got 6", id="window-6"),
        pytest.param({"recovery_window_days": 31}, "0 .* or 7-30, got 31", id="window-31"),
    ],
)
def test_aws_refuses_a_setting_it_cannot_store_under(fields, message):
    with pytest.raises(ValidationError, match=message):
        SecretsSettings.model_validate(_AWS | fields)


@pytest.mark.parametrize(
    "prefix", ["acme/dfe/test/engine", "a_b+c=d.e@f-g/h", "a" * _LONGEST_PREFIX]
)
def test_aws_accepts_a_prefix_aws_can_name(prefix):
    assert SecretsSettings.model_validate(_AWS | {"name_prefix": prefix}).name_prefix == prefix


@pytest.mark.parametrize("window", [0, 7, 30])
def test_aws_accepts_each_window_aws_allows(window):
    settings = SecretsSettings.model_validate(_AWS | {"recovery_window_days": window})
    assert settings.recovery_window_days == window


@pytest.mark.parametrize("provider", ["gcp", "azure"])
def test_a_provider_whose_names_allow_no_slash_is_refused(provider):
    with pytest.raises(ValidationError, match=f"DFE_SECRETS_PROVIDER={provider} is not supported"):
        SecretsSettings(provider=provider)


def test_the_file_provider_ignores_the_aws_fields(tmp_path):
    """No aws check runs for the file provider, and its secrets land under its root, unprefixed."""
    settings = SecretsSettings(
        provider="file", path=str(tmp_path), name_prefix="/not checked/", recovery_window_days=3
    )

    build_secrets(settings).put("ch/orgs/acme", "pw")

    assert (tmp_path / "ch" / "orgs" / "acme").read_text(encoding="utf-8") == "pw"
