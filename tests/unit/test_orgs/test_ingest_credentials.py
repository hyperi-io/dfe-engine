#  Project:      dfe-engine
#  File:         tests/unit/test_orgs/test_ingest_credentials.py
#  Purpose:      The ingest credential store, its tokens and the bindings compiled from it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Ingest credentials: stored beside the orgs, tokens in the secrets backend, bound as tenant ids."""

import pytest

from dfe_engine.orgs.ingest_credentials import (
    BEARER_LISTENERS,
    CredentialIdTakenError,
    IngestCredential,
)
from dfe_engine.orgs.ingest_tokens import (
    accepted_credentials,
    bearer_entries,
    bearer_secret_path,
    credential_secret_path,
    drop_token,
    mint_token,
    store_token,
    sync_bearer_tokens,
)
from dfe_engine.orgs.registry import OrgBoundError, OrgRegistry
from dfe_engine.orgs.seed import ensure_default_org
from dfe_engine.orgs.tenancy import Tenancy, compile_ingest_bindings
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import SecretsSettings


def _credential(name: str = "acme-agents", org: str = "acme", **fields) -> IngestCredential:
    return IngestCredential.model_validate(
        {
            "name": name,
            "org": org,
            "orgs": fields.pop("orgs", [org]),
            "default_org": fields.pop("default_org", org),
            "listeners": fields.pop("listeners", ["http"]),
            **fields,
        }
    )


@pytest.fixture
def registry(tmp_path) -> OrgRegistry:
    registry = OrgRegistry(tmp_path / "orgs")
    ensure_default_org(registry)
    registry.create("acme", org_ids=["acme-prod", "acme-dev"])
    registry.create("beta")
    return registry


@pytest.fixture
def secrets(tmp_path):
    return build_secrets(SecretsSettings(provider="file", path=str(tmp_path / "secrets")))


class TestStore:
    def test_a_credential_round_trips_without_its_id_in_the_body(self, registry):
        credential = _credential(orgs=["acme", "beta"], listeners=["http", "otlp"])
        registry.ingest_credentials.put(credential)

        assert registry.ingest_credentials.get("acme-agents") == credential
        stored = (registry._dir / "ingest-credentials" / "acme-agents.yaml").read_text(
            encoding="utf-8"
        )
        assert "name:" not in stored

    def test_the_credentials_are_not_read_as_orgs(self, registry):
        registry.ingest_credentials.put(_credential())

        assert {org.name for org in registry.list()} == {"default", "acme", "beta"}

    def test_an_id_another_org_holds_is_refused(self, registry):
        registry.ingest_credentials.put(_credential())

        with pytest.raises(CredentialIdTakenError, match="unique across every org"):
            registry.ingest_credentials.put(_credential(org="beta"))

    def test_owned_and_binding_are_told_apart(self, registry):
        registry.ingest_credentials.put(_credential(orgs=["acme", "beta"]))
        registry.ingest_credentials.put(_credential("beta-hec", org="beta"))

        assert [c.name for c in registry.ingest_credentials.owned_by("beta")] == ["beta-hec"]
        assert [c.name for c in registry.ingest_credentials.binding("beta")] == ["acme-agents"]

    @pytest.mark.parametrize("bad", ["", "acme:prod", "-acme", "a" * 129])
    def test_an_id_the_receiver_could_not_key_is_refused(self, bad):
        # A ':' would split the <id>:<token> entry the receiver reads.
        with pytest.raises(ValueError):
            _credential(bad)

    def test_a_credential_names_at_least_one_org_and_one_listener(self):
        with pytest.raises(ValueError):
            _credential(orgs=[])
        with pytest.raises(ValueError):
            _credential(listeners=[])

    def test_every_write_reaches_the_org_listeners(self, registry):
        heard: list[str] = []
        registry.on_change(lambda: heard.append("changed"))

        registry.ingest_credentials.put(_credential())
        registry.ingest_credentials.delete("acme-agents")

        assert heard == ["changed", "changed"]


class TestOrgDelete:
    def test_an_org_takes_its_credentials_with_it(self, registry):
        registry.ingest_credentials.put(_credential())

        registry.delete("acme")

        assert registry.ingest_credentials.get("acme-agents") is None

    def test_an_org_another_org_s_credential_binds_is_kept(self, registry):
        registry.ingest_credentials.put(_credential(orgs=["acme", "beta"]))

        with pytest.raises(OrgBoundError, match="'acme-agents' of org 'acme'"):
            registry.delete("beta")
        assert registry.get("beta") is not None

    def test_every_credential_the_cascade_removes_is_reported(self, registry):
        removed: list[str] = []
        registry.on_credential_removed(removed.append)
        registry.ingest_credentials.put(_credential())
        registry.ingest_credentials.put(_credential("acme-hec"))
        registry.ingest_credentials.put(_credential("beta-hec", org="beta"))

        registry.delete("acme")

        assert removed == ["acme-agents", "acme-hec"]


class TestBindings:
    """The golden ``ingest_bindings`` block dfe-receiver holds each record to."""

    def test_the_golden_bindings(self, registry):
        registry.ingest_credentials.put(
            _credential(orgs=["acme", "beta"], default_org="beta", listeners=["http", "otlp"])
        )
        registry.ingest_credentials.put(_credential("beta-hec", org="beta"))

        assert compile_ingest_bindings(Tenancy.read(registry)) == {
            "credentials": {
                "acme-agents": {
                    "orgs": ["acme-prod", "acme-dev", "beta"],
                    "default_org": "beta",
                },
                "beta-hec": {"orgs": ["beta"], "default_org": "beta"},
            }
        }

    def test_the_default_is_the_default_org_s_first_tenant_id(self, registry):
        registry.ingest_credentials.put(_credential())

        bound = compile_ingest_bindings(Tenancy.read(registry))["credentials"]["acme-agents"]

        assert bound == {"orgs": ["acme-prod", "acme-dev"], "default_org": "acme-prod"}

    def test_an_org_listed_twice_lists_its_tenant_ids_once(self, registry):
        registry.ingest_credentials.put(_credential(orgs=["acme", "beta", "acme"]))

        bound = compile_ingest_bindings(Tenancy.read(registry))["credentials"]["acme-agents"]

        assert bound["orgs"] == ["acme-prod", "acme-dev", "beta"]

    def test_an_org_s_new_tenant_id_reaches_the_binding(self, registry):
        registry.ingest_credentials.put(_credential())
        registry.update("acme", org_ids=["acme-eu"])

        bound = compile_ingest_bindings(Tenancy.read(registry))["credentials"]["acme-agents"]

        assert bound == {"orgs": ["acme-eu"], "default_org": "acme-eu"}

    def test_a_credential_naming_an_unregistered_org_is_left_unbound(self, registry):
        # Unbound, its records land in the default org alone.
        tenancy = Tenancy(orgs=tuple(registry.list()), credentials=(_credential(org="gone"),))

        assert compile_ingest_bindings(tenancy) == {"credentials": {}}

    def test_every_binding_is_one_the_receiver_accepts(self, registry):
        from dfe_engine.services.models.receiver import IngestBindingsConfig

        registry.ingest_credentials.put(_credential(orgs=["acme", "beta"], default_org="beta"))

        compiled = compile_ingest_bindings(Tenancy.read(registry))

        assert IngestBindingsConfig.model_validate(compiled).model_dump() == {
            **compiled,
            "listeners": {},
        }


class TestTokens:
    def test_a_minted_token_holds_neither_of_the_receiver_s_separators(self):
        tokens = {mint_token() for _ in range(50)}

        assert len(tokens) == 50
        assert not any(":" in token or "\n" in token for token in tokens)
        assert min(len(token) for token in tokens) >= 43

    def test_each_listener_gets_a_line_per_credential_it_accepts(self, registry, secrets):
        both = _credential(listeners=["http", "otlp"])
        hec = _credential("beta-hec", org="beta", listeners=["splunk_hec"])
        store_token(secrets, both.name, "token-a")
        store_token(secrets, hec.name, "token-b")

        entries = bearer_entries([hec, both], secrets)

        assert entries["http"] == "acme-agents:token-a\n"
        assert entries["otlp"] == "acme-agents:token-a\n"
        assert entries["splunk_hec"] == "beta-hec:token-b\n"
        assert set(entries) == set(BEARER_LISTENERS)

    def test_a_listener_no_credential_names_holds_no_token(self, secrets):
        # Never empty, so a backend that refuses an empty value still takes it.
        assert bearer_entries([], secrets) == dict.fromkeys(BEARER_LISTENERS, "\n")

    def test_a_credential_with_no_stored_token_is_left_out(self, secrets):
        assert bearer_entries([_credential()], secrets)["http"] == "\n"

    def test_a_sync_rewrites_only_what_changed(self, registry, secrets):
        credential = _credential()
        registry.ingest_credentials.put(credential)
        store_token(secrets, credential.name, "token-a")

        first = sync_bearer_tokens(registry, secrets)
        second = sync_bearer_tokens(registry, secrets)

        assert first == list(BEARER_LISTENERS)
        assert second == []
        assert secrets.get(bearer_secret_path("http")) == "acme-agents:token-a\n"

    def test_a_deleted_credential_s_token_leaves_every_listener(self, registry, secrets):
        credential = _credential(listeners=list(BEARER_LISTENERS))
        registry.ingest_credentials.put(credential)
        store_token(secrets, credential.name, "token-a")
        sync_bearer_tokens(registry, secrets)

        registry.ingest_credentials.delete(credential.name)
        drop_token(secrets, credential.name)
        sync_bearer_tokens(registry, secrets)

        assert secrets.get_or_none(credential_secret_path(credential.name)) is None
        for listener in BEARER_LISTENERS:
            assert "token-a" not in secrets.get(bearer_secret_path(listener))

    def test_a_disabled_org_s_credential_is_not_accepted(self, registry, secrets):
        registry.ingest_credentials.put(_credential())
        registry.ingest_credentials.put(_credential("beta-hec", org="beta"))
        registry.update("acme", enabled=False)

        assert [c.name for c in accepted_credentials(registry)] == ["beta-hec"]
