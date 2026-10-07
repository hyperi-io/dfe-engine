#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_okta_adapter.py
#  Purpose:      Tests for OktaAdapter
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
import stamina

from dfe_engine.auth.oidc.adapters.base import DirectoryError
from dfe_engine.auth.oidc.adapters.okta import OktaAdapter
from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import SecretsSettings
from tests.unit.test_auth.factories import (
    OKTA_API_TOKEN_ENV,
    make_okta_adapter,
    make_okta_directory_provider,
)
from tests.unit.test_auth.test_oidc.local_directory import LocalDirectory
from tests.unit.test_auth.test_oidc.okta_adapter_cases import (
    CONNECTION_CHECK_CASES,
    LIST_ALL_GROUPS_CASES,
    LIST_ALL_GROUPS_MISSING_SETTING_CASES,
    LIST_ALL_GROUPS_RAISES_CASES,
    TOKEN_FRAGMENT,
    UNSENDABLE_TOKEN_CASES,
    ConnectionCheckCase,
    ListAllGroupsCase,
    ListAllGroupsMissingSettingCase,
    ListAllGroupsRaisesCase,
    UnsendableTokenCase,
)


def _leaks(*, caplog: pytest.LogCaptureFixture, log_lines: list[str]) -> list[str]:
    """Every stdlib log record and loguru line that carries TOKEN_FRAGMENT, with all its fields."""
    records = [str(record.__dict__) for record in caplog.records]
    return [line for line in [*records, *log_lines] if TOKEN_FRAGMENT in line]


def _okta_provider(*, mode: str = "token_claim") -> OIDCProvider:
    return OIDCProvider(
        type="okta",
        issuer="https://example.okta.com",
        client_id_env="OKTA_CLIENT_ID",
        client_secret_env="OKTA_CLIENT_SECRET",
        groups=GroupResolutionConfig(mode=mode, claim_name="groups"),
    )


@pytest.fixture
def retrying_http() -> Iterator[None]:
    """Let the HTTP client retry once with no wait, so its retry hooks run and log as in production."""
    with stamina.set_testing(True, attempts=2):
        yield


class TestListAllGroups:
    @pytest.mark.usefixtures("okta_api_token")
    @pytest.mark.parametrize(
        "case", LIST_ALL_GROUPS_CASES, ids=[case["id"] for case in LIST_ALL_GROUPS_CASES]
    )
    async def test_matches_expected(self, tls_directory: LocalDirectory, case: ListAllGroupsCase):
        tls_directory.replies = case["replies"]
        adapter = make_okta_adapter(
            provider=make_okta_directory_provider(okta_domain=tls_directory.host)
        )
        assert await adapter.list_all_groups() == case["expected_groups"]

    @pytest.mark.usefixtures("okta_api_token")
    async def test_invalid_domain(self):
        adapter = make_okta_adapter(
            provider=make_okta_directory_provider(okta_domain="example\x00.okta.com")
        )
        message = "okta directory: GET '/api/v1/groups' failed: InvalidURL"
        with pytest.raises(DirectoryError, match=f"^{re.escape(message)}$"):
            await adapter.list_all_groups()

    @pytest.mark.parametrize(
        "case",
        LIST_ALL_GROUPS_MISSING_SETTING_CASES,
        ids=[case["id"] for case in LIST_ALL_GROUPS_MISSING_SETTING_CASES],
    )
    async def test_missing_setting(self, case: ListAllGroupsMissingSettingCase):
        with pytest.raises(DirectoryError, match=re.escape(case["message"])):
            await make_okta_adapter(provider=case["provider"]).list_all_groups()

    @pytest.mark.usefixtures("okta_api_token")
    @pytest.mark.parametrize(
        "case",
        LIST_ALL_GROUPS_RAISES_CASES,
        ids=[case["id"] for case in LIST_ALL_GROUPS_RAISES_CASES],
    )
    async def test_raises(self, tls_directory: LocalDirectory, case: ListAllGroupsRaisesCase):
        tls_directory.replies = case["replies"]
        adapter = make_okta_adapter(
            provider=make_okta_directory_provider(okta_domain=tls_directory.host)
        )
        with pytest.raises(DirectoryError, match=re.escape(case["message"])):
            await adapter.list_all_groups()

    @pytest.mark.usefixtures("okta_api_token")
    async def test_unreachable(self):
        # Nothing listens on port 1, so the connection is refused before any TLS or HTTP.
        adapter = make_okta_adapter(
            provider=make_okta_directory_provider(okta_domain="127.0.0.1:1")
        )
        message = "okta directory: GET '/api/v1/groups' failed: ConnectError"
        with pytest.raises(DirectoryError, match=re.escape(message)):
            await adapter.list_all_groups()


class TestOktaAdapterTestConnection:
    async def test_token_claim_mode_succeeds_without_api(self):
        adapter = OktaAdapter(_okta_provider(mode="token_claim"))
        ok, message = await adapter.test_connection()
        assert ok is True
        assert "token_claim" in message
        assert "verify-login" in message

    async def test_api_mode_missing_domain_fails(self):
        adapter = OktaAdapter(_okta_provider(mode="api"))
        ok, message = await adapter.test_connection()
        assert ok is False
        assert "okta_domain" in message

    async def test_api_mode_missing_token_fails(self, monkeypatch):
        provider = _okta_provider(mode="api")
        provider.groups.okta_domain = "example.okta.com"
        provider.groups.api_token_env = "OKTA_API_TOKEN"
        monkeypatch.delenv("OKTA_API_TOKEN", raising=False)
        adapter = OktaAdapter(provider)
        ok, message = await adapter.test_connection()
        assert ok is False
        assert "OKTA_API_TOKEN" in message


class TestTestConnectionAgainstTheDirectory:
    @pytest.mark.usefixtures("okta_api_token")
    @pytest.mark.parametrize(
        "case", CONNECTION_CHECK_CASES, ids=[case["id"] for case in CONNECTION_CHECK_CASES]
    )
    async def test_reports_the_directory_answer(
        self, tls_directory: LocalDirectory, case: ConnectionCheckCase
    ):
        tls_directory.replies = case["replies"]
        adapter = make_okta_adapter(
            provider=make_okta_directory_provider(okta_domain=tls_directory.host)
        )
        assert await adapter.test_connection() == case["expected_result"]


class TestUnsendableApiToken:
    @pytest.mark.usefixtures("retrying_http")
    async def test_list_all_groups(
        self,
        caplog: pytest.LogCaptureFixture,
        log_lines: list[str],
        monkeypatch: pytest.MonkeyPatch,
        tls_directory: LocalDirectory,
    ):
        monkeypatch.setenv(OKTA_API_TOKEN_ENV, "secret-okta\nvalue-7f3a")
        adapter = make_okta_adapter(
            provider=make_okta_directory_provider(okta_domain=tls_directory.host)
        )
        message = (
            "okta directory: the API token holds a character an HTTP header cannot carry: a "
            "space, a control character or a non-ASCII one; re-send 'groups.api_token' to the "
            "provider API or fix the env var 'DFE_TEST_OKTA_API_TOKEN'"
        )
        with pytest.raises(DirectoryError, match=f"^{re.escape(message)}$"):
            await adapter.list_all_groups()
        assert _leaks(caplog=caplog, log_lines=log_lines) == []

    @pytest.mark.usefixtures("retrying_http")
    @pytest.mark.parametrize(
        "case", UNSENDABLE_TOKEN_CASES, ids=[case["id"] for case in UNSENDABLE_TOKEN_CASES]
    )
    async def test_stays_out_of_the_answer_and_the_log(
        self,
        caplog: pytest.LogCaptureFixture,
        log_lines: list[str],
        monkeypatch: pytest.MonkeyPatch,
        tls_directory: LocalDirectory,
        case: UnsendableTokenCase,
    ):
        monkeypatch.setenv(OKTA_API_TOKEN_ENV, case["token"])
        adapter = make_okta_adapter(
            provider=make_okta_directory_provider(okta_domain=tls_directory.host)
        )
        result = await case["call"](adapter)
        assert (result, _leaks(caplog=caplog, log_lines=log_lines)) == (
            case["expected_result"],
            [],
        )


class TestOktaApiTokenResolution:
    """The Groups API token resolves through the secret store before the env."""

    def _store(self, tmp_path):
        return build_secrets(SecretsSettings(provider="file", path=str(tmp_path / "secrets")))

    def test_stored_token_beats_the_env(self, tmp_path, monkeypatch):
        store = self._store(tmp_path)
        store.put("oidc/acme/groups_api_token", "from-store")
        monkeypatch.setenv("OKTA_API_TOKEN", "from-env")
        provider = _okta_provider(mode="api")
        provider.groups.okta_domain = "example.okta.com"
        provider.groups.api_token_path = "oidc/acme/groups_api_token"
        provider.groups.api_token_env = "OKTA_API_TOKEN"

        _, headers = OktaAdapter(provider, secrets=store)._api_base_and_headers()

        assert headers["Authorization"] == "SSWS from-store"

    def test_env_serves_when_nothing_is_stored(self, tmp_path, monkeypatch):
        store = self._store(tmp_path)
        monkeypatch.setenv("OKTA_API_TOKEN", "from-env")
        provider = _okta_provider(mode="api")
        provider.groups.okta_domain = "example.okta.com"
        provider.groups.api_token_path = "oidc/acme/groups_api_token"
        provider.groups.api_token_env = "OKTA_API_TOKEN"

        _, headers = OktaAdapter(provider, secrets=store)._api_base_and_headers()

        assert headers["Authorization"] == "SSWS from-env"
