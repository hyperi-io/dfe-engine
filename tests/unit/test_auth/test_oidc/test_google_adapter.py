#  Project:      DFE Engine
#  File:         tests/unit/test_auth/test_oidc/test_google_adapter.py
#  Purpose:      Unit tests for Google Workspace Admin SDK group adapter.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import os

import pytest
from scalo.logger import logger

from dfe_engine.auth.oidc.adapters.base import (
    DirectoryCredentialError,
    DirectoryNotConfiguredError,
    DirectoryPageLimitError,
    NoDirectoryListingError,
)
from dfe_engine.auth.oidc.adapters.google import GoogleAdapter
from dfe_engine.auth.oidc.models import GroupInfo, GroupResolutionConfig, OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.rp import OidcRelyingParty
from tests.unit.test_auth.test_oidc.local_cloud_identity import LocalCloudIdentity
from tests.unit.test_auth.test_oidc.local_directory_api import (
    LocalDirectoryApi,
    LocalDirectoryGoogle,
)
from tests.unit.test_auth.test_oidc.unreachable_store import UnreachableSecretStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Synthetic ids in the shape Google assigns: the Cloud Identity name is groups/<id>.
ADMINS_ID = "03abc1def2ghi3j"
VIEWERS_ID = "01k2l3m4n5o6p7q"
NESTED_PARENT_ID = "04r5s6t7u8v9w0x"

TRANSITIVE = "searchTransitiveGroups"
DIRECT = "searchDirectGroups"

SCOPE_REFUSED = {
    "error": {
        "code": 403,
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
            }
        ],
        "message": "Request had insufficient authentication scopes.",
        "status": "PERMISSION_DENIED",
    }
}


def _make_provider(
    service_account_json_env: str = "DFE_GOOGLE_SA_JSON",
    domain: str = "example.com",
) -> OIDCProvider:
    return OIDCProvider(
        type="google",
        display_name="Google Workspace",
        issuer="https://accounts.google.com",
        client_id_env="DFE_OIDC_GOOGLE_CLIENT_ID",
        groups=GroupResolutionConfig(
            service_account_json_env=service_account_json_env,
            domain=domain,
        ),
    )


GROUP_LABEL = "cloudidentity.googleapis.com/groups.discussion_forum"


def _relation(
    group_id: str, name: str, *, labels: dict[str, str] | None = None
) -> dict[str, object]:
    """One Cloud Identity membership search result, as the API returns it: a group's carries its label."""
    return {
        "description": "",
        "displayName": name,
        "group": f"groups/{group_id}",
        "groupKey": {"id": f"{name}@example.com"},
        "labels": {GROUP_LABEL: ""} if labels is None else labels,
        "membership": f"groups/{group_id}/memberships/112233",
        "roles": [{"name": "MEMBER"}],
    }


@pytest.fixture
def cloud_identity():
    server = LocalCloudIdentity()
    server.start()
    yield server
    server.stop()


def _token_adapter(server: LocalCloudIdentity, *, token: str = "user-token") -> GoogleAdapter:  # noqa: S107
    """A Google adapter with no service account that reads Cloud Identity at *server*."""
    adapter = GoogleAdapter(_make_provider(service_account_json_env=""), access_token=token)
    adapter.CLOUD_IDENTITY_BASE = server.base_url
    return adapter


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestGoogleAdapterConstructor:
    def test_stores_provider(self) -> None:
        provider = _make_provider()
        adapter = GoogleAdapter(provider)
        assert adapter._provider is provider

    def test_provider_name_accessible(self) -> None:
        provider = _make_provider()
        adapter = GoogleAdapter(provider)
        assert adapter._provider.type == "google"

    def test_groups_config_accessible(self) -> None:
        provider = _make_provider(domain="mycompany.com")
        adapter = GoogleAdapter(provider)
        assert adapter._provider.groups.domain == "mycompany.com"

    def test_no_service_account_env_set(self) -> None:
        # Provider with no env var name configured
        provider = _make_provider(service_account_json_env="")
        adapter = GoogleAdapter(provider)
        assert adapter._provider.groups.service_account_json_env == ""


# ---------------------------------------------------------------------------
# _get_service — missing credentials
# ---------------------------------------------------------------------------


class TestGetServiceMissingCredentials:
    def test_returns_none_when_env_var_name_not_configured(self) -> None:
        provider = _make_provider(service_account_json_env="")
        adapter = GoogleAdapter(provider)
        service = adapter._get_service()
        assert service is None

    def test_returns_none_when_env_var_not_set(self) -> None:
        # Env var name is configured but the env var itself is absent
        provider = _make_provider(service_account_json_env="DFE_NONEXISTENT_SA_JSON_XYZ")
        adapter = GoogleAdapter(provider)
        # Ensure it's absent
        os.environ.pop("DFE_NONEXISTENT_SA_JSON_XYZ", None)
        service = adapter._get_service()
        assert service is None

    def test_returns_none_when_env_var_contains_invalid_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DFE_GOOGLE_SA_JSON", "not-valid-json")
        provider = _make_provider(service_account_json_env="DFE_GOOGLE_SA_JSON")
        adapter = GoogleAdapter(provider)
        service = adapter._get_service()
        assert service is None

    def test_returns_none_when_env_var_contains_wrong_json_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Valid JSON but not a service account dict
        monkeypatch.setenv("DFE_GOOGLE_SA_JSON", '["not", "a", "dict"]')
        provider = _make_provider(service_account_json_env="DFE_GOOGLE_SA_JSON")
        adapter = GoogleAdapter(provider)
        service = adapter._get_service()
        assert service is None


# ---------------------------------------------------------------------------
# resolve_user_groups (login enrichment) — missing credentials
# ---------------------------------------------------------------------------


class TestResolveUserGroupsMissingCredentials:
    async def test_returns_empty_list_when_no_credentials(self) -> None:
        # Google login enrichment fails open to default deny, never raises.
        provider = _make_provider(service_account_json_env="")
        adapter = GoogleAdapter(provider)
        result = await adapter.resolve_user_groups("user@example.com")
        assert result == []

    async def test_empty_user_key_short_circuits(self) -> None:
        provider = _make_provider(service_account_json_env="")
        adapter = GoogleAdapter(provider)
        assert await adapter.resolve_user_groups("") == []


# ---------------------------------------------------------------------------
# list_all_groups — missing credentials
# ---------------------------------------------------------------------------


class TestListAllGroupsMissingCredentials:
    async def test_no_service_account_has_no_directory_to_list(self) -> None:
        """The default: each login reads its own groups, which is no fault to report."""
        provider = _make_provider(service_account_json_env="")
        adapter = GoogleAdapter(provider)
        with pytest.raises(NoDirectoryListingError):
            await adapter.list_all_groups()

    async def test_refuses_to_list_when_env_var_missing(self) -> None:
        os.environ.pop("DFE_MISSING_SA_JSON_DEF", None)
        provider = _make_provider(service_account_json_env="DFE_MISSING_SA_JSON_DEF")
        adapter = GoogleAdapter(provider)
        with pytest.raises(DirectoryNotConfiguredError):
            await adapter.list_all_groups()


# ---------------------------------------------------------------------------
# test_connection — missing credentials
# ---------------------------------------------------------------------------


class TestConnection:
    async def test_no_service_account_is_a_working_setup(self) -> None:
        # Logins read groups with the user's own token, so there is nothing to probe.
        success, message = await GoogleAdapter(
            _make_provider(service_account_json_env="")
        ).test_connection()
        assert success is True
        assert "own token" in message

    async def test_a_configured_service_account_that_resolves_to_nothing_fails(self) -> None:
        os.environ.pop("DFE_MISSING_SA_JSON_GHI", None)
        provider = _make_provider(service_account_json_env="DFE_MISSING_SA_JSON_GHI")
        success, message = await GoogleAdapter(provider).test_connection()
        assert success is False
        assert "service account" in message.lower()

    async def test_an_unparseable_service_account_fails(self, monkeypatch) -> None:
        monkeypatch.setenv("DFE_GOOGLE_SA_JSON", "not-valid-json")
        success, message = await GoogleAdapter(_make_provider()).test_connection()
        assert success is False
        assert "service account" in message.lower()


def _stored_service_account_provider() -> OIDCProvider:
    """A Google provider whose service account JSON is held only in the secret store."""
    return OIDCProvider(
        type="google",
        issuer="https://accounts.google.com",
        groups=GroupResolutionConfig(
            mode="api", service_account_json_path="oidc/acme/groups_service_account_json"
        ),
    )


class TestSecretStoreDidNotAnswer:
    """A service account whose secret store is down exists all the same: an error, not 'not configured'."""

    async def test_the_listing_fails_as_an_error_and_the_login_lookup_still_answers(self):
        adapter = GoogleAdapter(
            _stored_service_account_provider(), secrets=UnreachableSecretStore()
        )

        with pytest.raises(DirectoryCredentialError):
            await adapter.list_all_groups()
        assert await adapter.resolve_user_groups("user@example.com") == []

    async def test_the_connection_test_names_the_secret_store(self):
        adapter = GoogleAdapter(
            _stored_service_account_provider(), secrets=UnreachableSecretStore()
        )

        ok, message = await adapter.test_connection()

        assert ok is False
        assert "secret store did not answer" in message
        assert "resolves to nothing" not in message


class TestServiceAccountFailuresLogNoText:
    """The parser's and google-auth's text can quote the service account, so only the class is logged."""

    @pytest.mark.parametrize(
        ("service_account", "event", "error_type"),
        [
            ("not-valid-json", "Could not parse service account JSON", "JSONDecodeError"),
            (
                '{"type": "service_account"}',
                "Failed to build Google Admin SDK service",
                "MalformedError",
            ),
        ],
        ids=["unparseable", "does-not-build"],
    )
    def test_a_service_account_that_does_not_build_logs_its_class(
        self, audit_events, monkeypatch, service_account, event, error_type
    ):
        monkeypatch.setenv("DFE_GOOGLE_SA_JSON", service_account)

        assert GoogleAdapter(_make_provider())._get_service() is None

        (line,) = [e for e in audit_events if e["event"] == event]
        assert "error" not in line
        assert line["error_type"] == error_type


@pytest.fixture
def endless_directory():
    """A Directory API that names a next page on each of ten pages."""
    directory = LocalDirectoryApi(pages=10)
    directory.start()
    yield directory
    directory.stop()


class TestDirectoryListingPageLimit:
    def test_a_listing_still_paging_at_the_limit_is_abandoned(self, endless_directory):
        adapter = GoogleAdapter(_make_provider())
        adapter.MAX_LISTING_PAGES = 3

        with pytest.raises(DirectoryPageLimitError):
            adapter._fetch_all_groups_sync(endless_directory.service())
        assert endless_directory.served == 3


class TestLoginLookupPageLimit:
    async def test_a_user_lookup_still_paging_at_the_limit_is_default_deny(
        self, endless_directory, audit_events
    ):
        adapter = LocalDirectoryGoogle(_make_provider(), base_url=endless_directory.base_url)
        adapter.MAX_LOGIN_PAGES = 3

        groups = await adapter.resolve_user_groups("user@example.com")

        assert groups == []
        assert endless_directory.served == 3
        (line,) = [e for e in audit_events if e.get("operation") == "resolve_user_groups"]
        assert line["error_type"] == "DirectoryPageLimitError"

    async def test_a_user_token_search_still_paging_at_the_limit_is_default_deny(
        self, cloud_identity, audit_events
    ):
        """The pages read so far are not the user's groups, as Okta's and Entra's lookups hold."""
        cloud_identity.pages[TRANSITIVE] = [[_relation(f"0{n}group", f"g{n}")] for n in range(6)]
        adapter = _token_adapter(cloud_identity)
        adapter.MAX_LOGIN_PAGES = 3

        groups = await adapter.resolve_user_groups("alice@example.com")

        assert groups == []
        assert cloud_identity.methods == [TRANSITIVE] * 3
        (line,) = [
            e
            for e in audit_events
            if e["event"] == "Google Cloud Identity: group search stopped at the page limit"
        ]
        assert (line["method"], line["pages"]) == (TRANSITIVE, 3)


# ---------------------------------------------------------------------------
# GroupInfo model used by adapter
# ---------------------------------------------------------------------------


class TestGroupInfo:
    def test_required_fields(self) -> None:
        group = GroupInfo(id="12345", name="Engineering")
        assert group.id == "12345"
        assert group.name == "Engineering"
        assert group.email == ""

    def test_optional_email(self) -> None:
        group = GroupInfo(id="12345", name="Engineering", email="eng@example.com")
        assert group.email == "eng@example.com"


# ---------------------------------------------------------------------------
# Cloud Identity with the user's token, against a local stand-in
# ---------------------------------------------------------------------------


class TestCloudIdentityUserToken:
    """The default login lookup: the user's own token, no service account."""

    async def test_reads_every_page_and_keys_groups_by_directory_id(self, cloud_identity):
        cloud_identity.pages[TRANSITIVE] = [
            [_relation(ADMINS_ID, "dfe-admins")],
            [_relation(VIEWERS_ID, "dfe-viewers")],
        ]
        groups = await _token_adapter(cloud_identity).resolve_user_groups("alice@example.com")

        assert groups == [
            GroupInfo(id=ADMINS_ID, name="dfe-admins", email="dfe-admins@example.com"),
            GroupInfo(id=VIEWERS_ID, name="dfe-viewers", email="dfe-viewers@example.com"),
        ]
        assert [request["params"].get("pageToken") for request in cloud_identity.requests] == [
            None,
            "1",
        ]

    async def test_sends_the_user_token_and_a_labelled_member_query(self, cloud_identity):
        await _token_adapter(cloud_identity, token="tok-123").resolve_user_groups(
            "alice@example.com"
        )

        request = cloud_identity.requests[0]
        assert request["authorization"] == "Bearer tok-123"
        assert request["params"]["query"] == (
            "member_key_id == 'alice@example.com' && "
            "'cloudidentity.googleapis.com/groups.discussion_forum' in labels"
        )

    async def test_a_quote_in_the_email_cannot_close_the_query_string(self, cloud_identity):
        await _token_adapter(cloud_identity).resolve_user_groups("o'brien@example.com")
        assert cloud_identity.requests[0]["params"]["query"].startswith(
            "member_key_id == 'o\\'brien@example.com' && "
        )

    async def test_a_transitive_answer_names_nested_groups_without_a_direct_search(
        self, cloud_identity
    ):
        cloud_identity.pages[TRANSITIVE] = [
            [_relation(ADMINS_ID, "dfe-admins"), _relation(NESTED_PARENT_ID, "dfe-nested-parent")]
        ]
        groups = await _token_adapter(cloud_identity).resolve_user_groups("alice@example.com")

        assert [group.id for group in groups] == [ADMINS_ID, NESTED_PARENT_ID]
        assert cloud_identity.methods == [TRANSITIVE]

    async def test_a_refused_transitive_search_falls_back_to_direct_groups(self, cloud_identity):
        # Transitive search is served only on Enterprise and Cloud Identity Premium editions.
        cloud_identity.refuse[TRANSITIVE] = (403, SCOPE_REFUSED)
        cloud_identity.pages[DIRECT] = [[_relation(VIEWERS_ID, "dfe-viewers")]]
        groups = await _token_adapter(cloud_identity).resolve_user_groups("alice@example.com")

        assert [group.id for group in groups] == [VIEWERS_ID]
        assert cloud_identity.methods == [TRANSITIVE, DIRECT]

    async def test_the_direct_fallback_asks_by_member_and_keeps_only_groups(self, cloud_identity):
        """Google refuses a direct search that filters on labels, so the label is checked here."""
        cloud_identity.refuse[TRANSITIVE] = (403, SCOPE_REFUSED)
        cloud_identity.pages[DIRECT] = [
            [
                _relation(ADMINS_ID, "dfe-admins"),
                _relation(NESTED_PARENT_ID, "not-a-group", labels={}),
                {
                    key: value
                    for key, value in _relation("05nolabel", "x").items()
                    if key != "labels"
                },
                _relation(VIEWERS_ID, "dfe-viewers"),
            ]
        ]

        groups = await _token_adapter(cloud_identity).resolve_user_groups("alice@example.com")

        assert [group.id for group in groups] == [ADMINS_ID, VIEWERS_ID]
        direct = cloud_identity.requests[1]
        assert (direct["method"], direct["params"]["query"]) == (
            DIRECT,
            "member_key_id == 'alice@example.com'",
        )

    async def test_a_refused_direct_search_denies_and_logs_googles_code(self, cloud_identity):
        cloud_identity.refuse[TRANSITIVE] = (403, SCOPE_REFUSED)
        cloud_identity.refuse[DIRECT] = (403, SCOPE_REFUSED)
        warnings: list[dict[str, object]] = []
        handler = logger.add(lambda m: warnings.append(m.record["extra"]), level="WARNING")
        try:
            groups = await _token_adapter(cloud_identity).resolve_user_groups("alice@example.com")
        finally:
            logger.remove(handler)

        assert groups == []
        assert [(w.get("status"), w.get("operation")) for w in warnings] == [(403, DIRECT)]
        assert warnings[0]["code"] == "PERMISSION_DENIED;ACCESS_TOKEN_SCOPE_INSUFFICIENT"

    async def test_a_rejected_token_does_not_try_the_direct_search(self, cloud_identity):
        cloud_identity.refuse[TRANSITIVE] = (401, {"error": {"status": "UNAUTHENTICATED"}})
        groups = await _token_adapter(cloud_identity).resolve_user_groups("alice@example.com")

        assert groups == []
        assert cloud_identity.methods == [TRANSITIVE]

    async def test_a_result_without_a_group_resource_name_is_dropped(self, cloud_identity):
        malformed = [
            {**_relation(ADMINS_ID, "x"), "group": bad} for bad in ("x", "groups/", "groups/a/b")
        ]
        cloud_identity.pages[TRANSITIVE] = [[*malformed, _relation(VIEWERS_ID, "dfe-viewers")]]
        groups = await _token_adapter(cloud_identity).resolve_user_groups("alice@example.com")
        assert [group.id for group in groups] == [VIEWERS_ID]

    async def test_no_groups_is_an_answer_not_a_failure(self, cloud_identity):
        adapter = _token_adapter(cloud_identity)
        assert await adapter._groups_with_user_token(email="alice@example.com") == []

    async def test_a_non_json_answer_is_no_answer(self, cloud_identity):
        cloud_identity.garble.add(TRANSITIVE)
        adapter = _token_adapter(cloud_identity)
        assert await adapter._groups_with_user_token(email="alice@example.com") is None

    async def test_a_page_that_is_not_a_json_object_is_no_answer(self, cloud_identity):
        """The pages before it are not the user's groups, so login is default deny."""
        cloud_identity.pages[TRANSITIVE] = [
            [_relation(ADMINS_ID, "dfe-admins")],
            [_relation(VIEWERS_ID, "dfe-viewers")],
        ]
        cloud_identity.non_object_page[TRANSITIVE] = 1

        groups = await _token_adapter(cloud_identity).resolve_user_groups("alice@example.com")

        assert groups == []
        assert cloud_identity.methods == [TRANSITIVE, TRANSITIVE]

    async def test_a_refusal_is_no_answer(self, cloud_identity):
        cloud_identity.refuse[TRANSITIVE] = (401, {})
        adapter = _token_adapter(cloud_identity)
        assert await adapter._groups_with_user_token(email="alice@example.com") is None

    async def test_stops_at_the_page_limit_and_answers_nothing(self, cloud_identity):
        cloud_identity.pages[TRANSITIVE] = [
            [_relation(f"0{index:014d}", f"g{index}")] for index in range(60)
        ]
        groups = await _token_adapter(cloud_identity).resolve_user_groups("alice@example.com")
        assert (groups, len(cloud_identity.requests)) == ([], 50)

    async def test_without_a_token_nothing_is_asked(self, cloud_identity):
        groups = await _token_adapter(cloud_identity, token="").resolve_user_groups(
            "alice@example.com"
        )
        assert (groups, cloud_identity.requests) == ([], [])

    async def test_the_login_token_reaches_the_lookup(self, cloud_identity, tmp_path, monkeypatch):
        monkeypatch.setattr(GoogleAdapter, "CLOUD_IDENTITY_BASE", cloud_identity.base_url)
        cloud_identity.pages[TRANSITIVE] = [[_relation(ADMINS_ID, "dfe-admins")]]
        rp = OidcRelyingParty(OIDCProviderRegistry(tmp_path / "reg"))
        provider = OIDCProvider(
            type="google",
            issuer="https://accounts.google.com",
            groups=GroupResolutionConfig(enrich_on_login=True, mode="api"),
        )
        # The token response Authlib hands back once it has validated the id_token.
        token = {
            "access_token": "login-token",
            "userinfo": {"email": "alice@example.com", "name": "Alice", "sub": "1234567890"},
        }

        identity = await rp._identity_from_token(client=None, provider=provider, token=token)

        assert identity.groups == [ADMINS_ID]
        assert cloud_identity.requests[0]["authorization"] == "Bearer login-token"
