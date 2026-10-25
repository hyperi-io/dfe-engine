#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_rp_directory_fallback.py
#  Purpose:      An outage keeps a login's last known groups, and a refusal gives it none
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the RP does with each kind of directory group lookup failure.

An outage (no answer, a 429, a 5xx) keeps the groups the signed ID token carries,
else marks the identity for the account's stored ones. A refusal (a 401 or 403, a
declined scope) is judged as no directory groups, so a user cannot keep groups by
declining the scope the lookup needs. Each case runs the RP's token handling
against a real adapter pointed at a local Okta, Graph or Cloud Identity service.

A lookup still running at the login deadline is an outage. Those cases use a
directory that never answers, so they hold whatever the machine's speed.
"""

import asyncio
import json

from prometheus_client.parser import text_string_to_metric_families
from scalo.metrics import create_metrics

from dfe_engine.auth.oidc.adapters.base import LookupFailure
from dfe_engine.auth.oidc.adapters.google import GoogleAdapter
from dfe_engine.auth.oidc.models import GroupInfo, GroupResolutionConfig, OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.rp import (
    DEFAULT_DIRECTORY_DEADLINE_SECONDS,
    DIRECTORY_GROUPS_FALLBACKS,
    DIRECTORY_GROUPS_REFUSALS,
    LoginMetrics,
    OidcRelyingParty,
    build_relying_party,
)
from dfe_engine.settings import AuthSettings
from tests.unit.test_auth.test_oidc.local_cloud_identity import LocalCloudIdentity
from tests.unit.test_auth.test_oidc.local_directory import AppTokenEntra
from tests.unit.test_auth.test_oidc.local_graph import LocalGraph
from tests.unit.test_auth.test_oidc.local_okta import LocalOkta, okta_adapter

OKTA = OIDCProvider(
    type="okta",
    issuer="https://acme.okta.com",
    groups=GroupResolutionConfig(enrich_on_login=True, mode="api", okta_domain="acme.okta.com"),
)
ENTRA = OIDCProvider(type="entra_id", issuer="https://login.microsoftonline.com/tid/v2.0")
GOOGLE = OIDCProvider(
    type="google",
    issuer="https://accounts.google.com",
    groups=GroupResolutionConfig(enrich_on_login=True, mode="api"),
)
OID = "00000000-0000-0000-0000-00000000b1g0"
OUTAGE_EVENT = "OIDC RP: directory could not answer the group lookup, keeping the last known groups"
REFUSAL_EVENT = (
    "OIDC RP: directory refused the group lookup, judging the login on no directory groups"
)
ACCESS_TOKEN = "login-access-token-4471"  # noqa: S105, RUF100 - a test token
SCOPE_REFUSED = {
    "error": {"code": 403, "status": "PERMISSION_DENIED", "message": "insufficient scope"}
}

OKTA_TOKEN = {
    "access_token": ACCESS_TOKEN,
    "userinfo": {
        "email": "jane@acme.test",
        "groups": ["dfe-admins", "soc"],
        "name": "Jane",
        "sub": "00u1jane",
    },
}


def _counted(manager, name: str) -> dict[str, float]:
    """One login counter, by provider, as the engine's /metrics serves it."""
    return {
        sample.labels["provider"]: sample.value
        for family in text_string_to_metric_families(manager.metrics_text)
        for sample in family.samples
        if sample.name == name
    }


def _manager():
    return create_metrics("test", backend="prometheus", enable_auto_update=False)


def _rp(tmp_path, manager) -> OidcRelyingParty:
    return OidcRelyingParty(OIDCProviderRegistry(tmp_path / "reg"), metrics=LoginMetrics(manager))


def _rp_with_deadline(tmp_path, manager, seconds: float) -> OidcRelyingParty:
    rp = build_relying_party(
        OIDCProviderRegistry(tmp_path / "reg"),
        metrics=LoginMetrics(manager),
        directory_deadline_seconds=seconds,
    )
    assert rp is not None
    return rp


class _Directory:
    """A directory adapter that gives *answer*, or never answers when *answer* is None."""

    def __init__(self, answer: list[GroupInfo] | LookupFailure | None = None) -> None:
        self.answer = answer
        self.asked = 0

    async def resolve_user_groups(self, directory_id: str) -> list[GroupInfo] | LookupFailure:
        self.asked += 1
        if self.answer is None:
            await asyncio.Event().wait()
        assert self.answer is not None
        return self.answer


def _point_at(monkeypatch, directory: _Directory) -> None:
    monkeypatch.setattr(
        "dfe_engine.auth.oidc.adapters.get_adapter", lambda *_args, **_kwargs: directory
    )


class TestAnOutage:
    async def test_okta_failing_keeps_the_token_groups_counted_and_logged_once(
        self, tmp_path, monkeypatch, audit_events
    ):
        okta = LocalOkta(pages=[[]])
        okta.fail_page = 0
        okta.fail_status = 503
        okta.start()
        manager = _manager()
        monkeypatch.setattr(
            "dfe_engine.auth.oidc.adapters.get_adapter",
            lambda *_args, **_kwargs: okta_adapter(okta.base_url),
        )
        try:
            identity = await _rp(tmp_path, manager)._identity_from_token(
                client=None, provider=OKTA, provider_name="acme-okta", token=OKTA_TOKEN
            )
        finally:
            okta.stop()

        assert (identity.groups, identity.groups_unresolved) == (["dfe-admins", "soc"], False)
        assert _counted(manager, DIRECTORY_GROUPS_FALLBACKS) == {"acme-okta": 1.0}
        assert _counted(manager, DIRECTORY_GROUPS_REFUSALS) == {}
        (line,) = [event for event in audit_events if event["event"] == OUTAGE_EVENT]
        assert (line["provider_name"], line["claimed_group_count"], line["kept"]) == (
            "acme-okta",
            2,
            "token_groups",
        )
        assert "jane@acme.test" not in json.dumps(line, default=str)
        assert ACCESS_TOKEN not in json.dumps(audit_events, default=str)

    async def test_an_overage_token_with_no_groups_claim_is_marked_for_the_stored_groups(
        self, tmp_path, monkeypatch, audit_events
    ):
        graph = LocalGraph()
        graph.refuse[f"users/{OID}"] = (503, {"error": {"code": "ServiceUnavailable"}})
        graph.start()
        adapter = AppTokenEntra(ENTRA)
        adapter.GRAPH_BASE = graph.base_url
        manager = _manager()
        monkeypatch.setattr(
            "dfe_engine.auth.oidc.adapters.get_adapter", lambda *_args, **_kwargs: adapter
        )
        token = {
            "userinfo": {
                "_claim_names": {"groups": "src1"},
                "email": "big@acme.test",
                "name": "Big User",
                "oid": OID,
                "sub": "pairwise-sub",
            },
        }
        try:
            identity = await _rp(tmp_path, manager)._identity_from_token(
                client=None, provider=ENTRA, provider_name="acme-entra", token=token
            )
        finally:
            graph.stop()

        assert (identity.groups, identity.groups_overflowed, identity.groups_unresolved) == (
            [],
            True,
            True,
        )
        assert _counted(manager, DIRECTORY_GROUPS_FALLBACKS) == {"acme-entra": 1.0}
        (line,) = [event for event in audit_events if event["event"] == OUTAGE_EVENT]
        assert (line["overflow"], line["claimed_group_count"], line["kept"]) == (
            True,
            0,
            "stored_groups",
        )


class TestARefusal:
    async def test_okta_refusing_clears_the_token_groups(self, tmp_path, monkeypatch, audit_events):
        okta = LocalOkta(pages=[[]])
        okta.fail_page = 0
        okta.fail_status = 403
        okta.fail_body = {"errorCode": "E0000006", "errorSummary": "denied"}
        okta.start()
        manager = _manager()
        monkeypatch.setattr(
            "dfe_engine.auth.oidc.adapters.get_adapter",
            lambda *_args, **_kwargs: okta_adapter(okta.base_url),
        )
        try:
            identity = await _rp(tmp_path, manager)._identity_from_token(
                client=None, provider=OKTA, provider_name="acme-okta", token=OKTA_TOKEN
            )
        finally:
            okta.stop()

        assert (identity.groups, identity.groups_unresolved) == ([], False)
        assert _counted(manager, DIRECTORY_GROUPS_REFUSALS) == {"acme-okta": 1.0}
        assert _counted(manager, DIRECTORY_GROUPS_FALLBACKS) == {}
        (line,) = [event for event in audit_events if event["event"] == REFUSAL_EVENT]
        assert (line["provider_name"], line["claimed_group_count"]) == ("acme-okta", 2)

    async def test_a_google_user_declining_the_groups_scope_keeps_nothing(
        self, tmp_path, monkeypatch
    ):
        """With no service account to ask instead, the user's own token is the only path."""
        cloud_identity = LocalCloudIdentity()
        cloud_identity.refuse["searchTransitiveGroups"] = (403, SCOPE_REFUSED)
        cloud_identity.refuse["searchDirectGroups"] = (403, SCOPE_REFUSED)
        cloud_identity.start()
        adapter = GoogleAdapter(GOOGLE, access_token=ACCESS_TOKEN)
        adapter.CLOUD_IDENTITY_BASE = cloud_identity.base_url
        manager = _manager()
        monkeypatch.setattr(
            "dfe_engine.auth.oidc.adapters.get_adapter", lambda *_args, **_kwargs: adapter
        )
        token = {
            "access_token": ACCESS_TOKEN,
            "userinfo": {"email": "alice@acme.test", "name": "Alice", "sub": "1234567890"},
        }
        try:
            identity = await _rp(tmp_path, manager)._identity_from_token(
                client=None, provider=GOOGLE, provider_name="acme-google", token=token
            )
        finally:
            cloud_identity.stop()

        assert (identity.groups, identity.groups_unresolved) == ([], False)
        assert _counted(manager, DIRECTORY_GROUPS_REFUSALS) == {"acme-google": 1.0}
        assert _counted(manager, DIRECTORY_GROUPS_FALLBACKS) == {}

    async def test_a_graph_page_with_no_value_list_is_a_refusal_not_an_empty_answer(
        self, tmp_path, monkeypatch, audit_events
    ):
        """The group sync refuses the same page, so a login never reads it as no groups either."""
        graph = LocalGraph()
        graph.refuse[f"users/{OID}"] = (200, {"@odata.context": "users/groups"})
        graph.start()
        adapter = AppTokenEntra(ENTRA)
        adapter.GRAPH_BASE = graph.base_url
        manager = _manager()
        monkeypatch.setattr(
            "dfe_engine.auth.oidc.adapters.get_adapter", lambda *_args, **_kwargs: adapter
        )
        token = {
            "userinfo": {
                "_claim_names": {"groups": "src1"},
                "email": "big@acme.test",
                "oid": OID,
                "sub": "pairwise-sub",
            },
        }
        try:
            identity = await _rp(tmp_path, manager)._identity_from_token(
                client=None, provider=ENTRA, provider_name="acme-entra", token=token
            )
        finally:
            graph.stop()

        assert identity.groups == []
        assert _counted(manager, DIRECTORY_GROUPS_REFUSALS) == {"acme-entra": 1.0}
        assert _counted(manager, DIRECTORY_GROUPS_FALLBACKS) == {}
        assert not [event for event in audit_events if "resolved groups" in event["event"]]


class TestADirectoryThatAnswers:
    async def test_an_empty_answer_replaces_the_token_groups(self, tmp_path, monkeypatch):
        """A directory that answers is the authority, so a user it puts in no group has none."""
        okta = LocalOkta(pages=[[]])
        okta.start()
        manager = _manager()
        monkeypatch.setattr(
            "dfe_engine.auth.oidc.adapters.get_adapter",
            lambda *_args, **_kwargs: okta_adapter(okta.base_url),
        )
        try:
            identity = await _rp(tmp_path, manager)._identity_from_token(
                client=None, provider=OKTA, provider_name="acme-okta", token=OKTA_TOKEN
            )
        finally:
            okta.stop()

        assert (identity.groups, identity.groups_unresolved) == ([], False)
        assert _counted(manager, DIRECTORY_GROUPS_FALLBACKS) == {}

    async def test_its_answer_is_used_and_nothing_is_counted(self, tmp_path, monkeypatch):
        okta = LocalOkta(pages=[[{"id": "00g1", "profile": {"name": "dfe-viewers"}}]])
        okta.start()
        manager = _manager()
        monkeypatch.setattr(
            "dfe_engine.auth.oidc.adapters.get_adapter",
            lambda *_args, **_kwargs: okta_adapter(okta.base_url),
        )
        try:
            identity = await _rp(tmp_path, manager)._identity_from_token(
                client=None, provider=OKTA, provider_name="acme-okta", token=OKTA_TOKEN
            )
        finally:
            okta.stop()

        assert identity.groups == ["dfe-viewers"]
        assert _counted(manager, DIRECTORY_GROUPS_FALLBACKS) == {}
        assert _counted(manager, DIRECTORY_GROUPS_REFUSALS) == {}


class TestTheLoginDeadline:
    def test_an_rp_built_without_settings_waits_the_settings_default(self, tmp_path):
        assert (
            DEFAULT_DIRECTORY_DEADLINE_SECONDS
            == AuthSettings().oidc_login_directory_deadline_seconds
        )
        assert (
            _rp(tmp_path, _manager())._directory_deadline_seconds
            == DEFAULT_DIRECTORY_DEADLINE_SECONDS
        )

    async def test_a_lookup_past_it_keeps_the_token_groups_counted_and_logged_once(
        self, tmp_path, monkeypatch, audit_events
    ):
        directory = _Directory()
        _point_at(monkeypatch, directory)
        manager = _manager()

        identity = await _rp_with_deadline(tmp_path, manager, 0.01)._identity_from_token(
            client=None, provider=OKTA, provider_name="acme-okta", token=OKTA_TOKEN
        )

        assert directory.asked == 1
        assert (identity.groups, identity.groups_unresolved) == (["dfe-admins", "soc"], False)
        assert _counted(manager, DIRECTORY_GROUPS_FALLBACKS) == {"acme-okta": 1.0}
        assert _counted(manager, DIRECTORY_GROUPS_REFUSALS) == {}
        (line,) = [event for event in audit_events if event["event"] == OUTAGE_EVENT]
        assert (line["provider_name"], line["kept"], line["error_type"]) == (
            "acme-okta",
            "token_groups",
            "TimeoutError",
        )
        assert line["deadline_seconds"] == 0.01
        assert line["elapsed_seconds"] >= 0.01

    async def test_an_overage_past_it_is_marked_for_the_stored_groups(
        self, tmp_path, monkeypatch, audit_events
    ):
        _point_at(monkeypatch, _Directory())
        manager = _manager()
        token = {"userinfo": {"_claim_names": {"groups": "src1"}, "oid": OID, "sub": "pairwise"}}

        identity = await _rp_with_deadline(tmp_path, manager, 0.01)._identity_from_token(
            client=None, provider=ENTRA, provider_name="acme-entra", token=token
        )

        assert (identity.groups, identity.groups_unresolved) == ([], True)
        assert _counted(manager, DIRECTORY_GROUPS_FALLBACKS) == {"acme-entra": 1.0}
        (line,) = [event for event in audit_events if event["event"] == OUTAGE_EVENT]
        assert line["kept"] == "stored_groups"

    async def test_an_answer_inside_it_is_used_and_nothing_is_counted(
        self, tmp_path, monkeypatch, audit_events
    ):
        _point_at(monkeypatch, _Directory([GroupInfo(id="00g1", name="dfe-viewers")]))
        manager = _manager()

        identity = await _rp(tmp_path, manager)._identity_from_token(
            client=None, provider=OKTA, provider_name="acme-okta", token=OKTA_TOKEN
        )

        assert (identity.groups, identity.groups_unresolved) == (["00g1"], False)
        assert _counted(manager, DIRECTORY_GROUPS_FALLBACKS) == {}
        assert _counted(manager, DIRECTORY_GROUPS_REFUSALS) == {}
        assert not [event for event in audit_events if event["event"] == OUTAGE_EVENT]

    async def test_a_refusal_inside_it_is_still_a_refusal(self, tmp_path, monkeypatch):
        _point_at(monkeypatch, _Directory(LookupFailure.REFUSED))
        manager = _manager()

        identity = await _rp(tmp_path, manager)._identity_from_token(
            client=None, provider=OKTA, provider_name="acme-okta", token=OKTA_TOKEN
        )

        assert (identity.groups, identity.groups_unresolved) == ([], False)
        assert _counted(manager, DIRECTORY_GROUPS_REFUSALS) == {"acme-okta": 1.0}
        assert _counted(manager, DIRECTORY_GROUPS_FALLBACKS) == {}
