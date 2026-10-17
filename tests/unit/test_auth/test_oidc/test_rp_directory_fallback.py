#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_rp_directory_fallback.py
#  Purpose:      A login whose directory group lookup fails keeps the ID token's groups
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A login whose directory group lookup gives no answer keeps the signed ID token's groups.

Each case runs the RP's token handling against a real adapter pointed at the
local Okta or Graph service, refusing the lookup, so the failure crosses the
real protocol and the real adapter turns it into no answer.
"""

import json

from prometheus_client.parser import text_string_to_metric_families
from scalo.metrics import create_metrics

from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.rp import DIRECTORY_GROUPS_FALLBACKS, LoginMetrics, OidcRelyingParty
from tests.unit.test_auth.test_oidc.local_directory import AppTokenEntra
from tests.unit.test_auth.test_oidc.local_graph import LocalGraph
from tests.unit.test_auth.test_oidc.local_okta import LocalOkta, okta_adapter

OKTA = OIDCProvider(
    type="okta",
    issuer="https://acme.okta.com",
    groups=GroupResolutionConfig(enrich_on_login=True, mode="api", okta_domain="acme.okta.com"),
)
ENTRA = OIDCProvider(type="entra_id", issuer="https://login.microsoftonline.com/tid/v2.0")
OID = "00000000-0000-0000-0000-00000000b1g0"
FALLBACK_EVENT = "OIDC RP: directory group lookup gave no answer, keeping the last known groups"
ACCESS_TOKEN = "login-access-token-4471"  # noqa: S105, RUF100 - a test token


def _fallbacks(manager) -> dict[str, float]:
    """The fallback counter, by provider, as the engine's /metrics serves it."""
    return {
        sample.labels["provider"]: sample.value
        for family in text_string_to_metric_families(manager.metrics_text)
        for sample in family.samples
        if sample.name == DIRECTORY_GROUPS_FALLBACKS
    }


def _rp(tmp_path, manager) -> OidcRelyingParty:
    return OidcRelyingParty(OIDCProviderRegistry(tmp_path / "reg"), metrics=LoginMetrics(manager))


class TestADirectoryThatGivesNoAnswer:
    async def test_okta_refusing_the_lookup_keeps_the_token_groups_counted_and_logged_once(
        self, tmp_path, monkeypatch, audit_events
    ):
        okta = LocalOkta(pages=[[]])
        okta.fail_page = 0
        okta.fail_status = 403
        okta.fail_body = {"errorCode": "E0000006", "errorSummary": "denied"}
        okta.start()
        manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
        monkeypatch.setattr(
            "dfe_engine.auth.oidc.adapters.get_adapter",
            lambda *_args, **_kwargs: okta_adapter(okta.base_url),
        )
        token = {
            "access_token": ACCESS_TOKEN,
            "userinfo": {
                "email": "jane@acme.test",
                "groups": ["dfe-admins", "soc"],
                "name": "Jane",
                "sub": "00u1jane",
            },
        }
        try:
            identity = await _rp(tmp_path, manager)._identity_from_token(
                client=None, provider=OKTA, provider_name="acme-okta", token=token
            )
        finally:
            okta.stop()

        assert (identity.groups, identity.groups_unresolved) == (["dfe-admins", "soc"], False)
        assert okta.pages_requested == [0]
        assert _fallbacks(manager) == {"acme-okta": 1.0}
        (line,) = [event for event in audit_events if event["event"] == FALLBACK_EVENT]
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
        graph.refuse[f"users/{OID}"] = (403, {"error": {"code": "Authorization_RequestDenied"}})
        graph.start()
        adapter = AppTokenEntra(ENTRA)
        adapter.GRAPH_BASE = graph.base_url
        manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
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
        assert graph.principals == [f"users/{OID}"]
        assert _fallbacks(manager) == {"acme-entra": 1.0}
        (line,) = [event for event in audit_events if event["event"] == FALLBACK_EVENT]
        assert (line["overflow"], line["claimed_group_count"], line["kept"]) == (
            True,
            0,
            "stored_groups",
        )


class TestADirectoryThatAnswers:
    async def test_an_empty_answer_replaces_the_token_groups(self, tmp_path, monkeypatch):
        """A directory that answers is the authority, so a user it puts in no group has none."""
        okta = LocalOkta(pages=[[]])
        okta.start()
        manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
        monkeypatch.setattr(
            "dfe_engine.auth.oidc.adapters.get_adapter",
            lambda *_args, **_kwargs: okta_adapter(okta.base_url),
        )
        token = {
            "userinfo": {
                "email": "jane@acme.test",
                "groups": ["dfe-admins"],
                "name": "Jane",
                "sub": "00u1jane",
            },
        }
        try:
            identity = await _rp(tmp_path, manager)._identity_from_token(
                client=None, provider=OKTA, provider_name="acme-okta", token=token
            )
        finally:
            okta.stop()

        assert (identity.groups, identity.groups_unresolved) == ([], False)
        assert _fallbacks(manager) == {}

    async def test_its_answer_is_used_and_nothing_is_counted(self, tmp_path, monkeypatch):
        okta = LocalOkta(pages=[[{"id": "00g1", "profile": {"name": "dfe-viewers"}}]])
        okta.start()
        manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
        monkeypatch.setattr(
            "dfe_engine.auth.oidc.adapters.get_adapter",
            lambda *_args, **_kwargs: okta_adapter(okta.base_url),
        )
        token = {
            "userinfo": {
                "email": "jane@acme.test",
                "groups": ["dfe-admins"],
                "name": "Jane",
                "sub": "00u1jane",
            },
        }
        try:
            identity = await _rp(tmp_path, manager)._identity_from_token(
                client=None, provider=OKTA, provider_name="acme-okta", token=token
            )
        finally:
            okta.stop()

        assert identity.groups == ["dfe-viewers"]
        assert _fallbacks(manager) == {}
