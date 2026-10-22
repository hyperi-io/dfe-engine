#  Project:      dfe-engine
#  File:         tests/unit/test_orgs/test_bearer_mirror.py
#  Purpose:      The receiver's bearer tokens mirrored into a Kubernetes Secret
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""With DFE_INGEST_BEARER_SECRET set, every listener's bearer entries also land in that Secret.

The engine writes it through scalo's ``kubernetes`` provider, here against a loopback
stand-in for the API server, and the receiver mounts it read-only.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from scalo.secrets import SecretPermissionError, SecretsError

from dfe_engine.orgs.ingest_credentials import BEARER_LISTENERS, IngestCredential
from dfe_engine.orgs.ingest_tokens import (
    bearer_secret_path,
    build_bearer_mirror,
    drop_token,
    resync_bearer_tokens,
    store_token,
    sync_bearer_tokens,
)
from dfe_engine.orgs.registry import OrgRegistry
from dfe_engine.orgs.seed import ensure_default_org
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import IngestSettings, SecretsSettings, _get_env_overrides
from tests.support.local_kube_api import LocalKubeApi, local_kube_api, mirror_for

# The stand-in's service account token: a fixed placeholder, never a credential.
SA_TOKEN = "service-account-token-placeholder"


@pytest.fixture
def registry(tmp_path) -> OrgRegistry:
    registry = OrgRegistry(tmp_path / "orgs")
    ensure_default_org(registry)
    registry.create("acme", org_ids=["acme-prod"])
    return registry


@pytest.fixture
def secrets(tmp_path):
    return build_secrets(SecretsSettings(provider="file", path=str(tmp_path / "secrets")))


@pytest.fixture
def token_file(tmp_path) -> Path:
    path = tmp_path / "sa-token"
    path.write_text(SA_TOKEN, encoding="utf-8")
    return path


@pytest.fixture
def kube() -> Iterator[LocalKubeApi]:
    with local_kube_api(SA_TOKEN) as api:
        yield api


def _credential(registry: OrgRegistry, secrets, listeners: list[str]) -> IngestCredential:
    credential = IngestCredential.model_validate(
        {
            "name": "acme-agents",
            "org": "acme",
            "orgs": ["acme"],
            "default_org": "acme",
            "listeners": listeners,
        }
    )
    registry.ingest_credentials.put(credential)
    store_token(secrets, credential.name, "token-a")
    return credential


class TestMirror:
    def test_every_listener_s_entries_reach_the_secret(self, registry, secrets, kube, token_file):
        _credential(registry, secrets, ["http", "otlp"])
        kube.create_empty()

        rewritten = sync_bearer_tokens(registry, secrets, mirror_for(kube, token_file))

        assert rewritten == list(BEARER_LISTENERS)
        assert kube.data() == {
            "http": "acme-agents:token-a\n",
            "grpc": "\n",
            "otlp": "acme-agents:token-a\n",
            "splunk_hec": "\n",
            "prometheus_rw": "\n",
        }

    def test_a_second_sync_writes_nothing(self, registry, secrets, kube, token_file):
        _credential(registry, secrets, ["http"])
        kube.create_empty()
        mirror = mirror_for(kube, token_file)
        sync_bearer_tokens(registry, secrets, mirror)
        puts = kube.puts()

        assert sync_bearer_tokens(registry, secrets, mirror) == []
        assert kube.puts() == puts

    def test_a_reset_secret_is_caught_up_though_the_backend_matches(
        self, registry, secrets, kube, token_file
    ):
        _credential(registry, secrets, ["http"])
        kube.create_empty()
        mirror = mirror_for(kube, token_file)
        sync_bearer_tokens(registry, secrets, mirror)

        kube.create_empty()

        assert sync_bearer_tokens(registry, secrets, mirror) == list(BEARER_LISTENERS)
        assert kube.data()["http"] == "acme-agents:token-a\n"

    def test_a_deleted_credential_leaves_every_key_and_no_key_is_removed(
        self, registry, secrets, kube, token_file
    ):
        credential = _credential(registry, secrets, list(BEARER_LISTENERS))
        kube.create_empty()
        mirror = mirror_for(kube, token_file)
        sync_bearer_tokens(registry, secrets, mirror)

        registry.ingest_credentials.delete(credential.name)
        drop_token(secrets, credential.name)
        sync_bearer_tokens(registry, secrets, mirror)

        # Each key stays, holding no token: the receiver keeps its last tokens when a file goes.
        assert kube.data() == dict.fromkeys(BEARER_LISTENERS, "\n")

    def test_a_missing_secret_fails_once_the_backend_is_written(
        self, registry, secrets, kube, token_file
    ):
        _credential(registry, secrets, ["http"])

        with pytest.raises(SecretsError, match="does not exist"):
            sync_bearer_tokens(registry, secrets, mirror_for(kube, token_file))

        assert secrets.get(bearer_secret_path("http")) == "acme-agents:token-a\n"
        assert kube.puts() == 0

    def test_a_secret_the_role_does_not_name_is_refused(self, registry, secrets, kube, token_file):
        _credential(registry, secrets, ["http"])
        kube.create_empty("someone-elses-secret")

        with pytest.raises(SecretPermissionError):
            sync_bearer_tokens(
                registry, secrets, mirror_for(kube, token_file, name="someone-elses-secret")
            )

    def test_with_no_mirror_the_api_server_is_never_called(self, registry, secrets, kube):
        _credential(registry, secrets, ["http"])
        kube.create_empty()

        assert sync_bearer_tokens(registry, secrets) == list(BEARER_LISTENERS)
        assert kube.requests == []


class TestResync:
    def test_a_secret_recreated_empty_is_refilled(self, registry, secrets, kube, token_file):
        _credential(registry, secrets, ["http"])
        kube.create_empty()
        mirror = mirror_for(kube, token_file)
        sync_bearer_tokens(registry, secrets, mirror)
        kube.create_empty()

        resync_bearer_tokens(registry, secrets, mirror)

        assert kube.data()["http"] == "acme-agents:token-a\n"

    def test_a_refused_resync_is_logged_never_raised(self, registry, secrets, kube, token_file):
        _credential(registry, secrets, ["http"])

        resync_bearer_tokens(registry, secrets, mirror_for(kube, token_file))

        assert kube.puts() == 0
        assert secrets.get(bearer_secret_path("http")) == "acme-agents:token-a\n"


class TestBuild:
    def test_unset_builds_no_mirror(self):
        assert build_bearer_mirror("") is None

    def test_set_builds_the_kubernetes_provider(self, monkeypatch):
        # Outside a pod the provider is configured but has no API server to reach.
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        monkeypatch.delenv("KUBERNETES_SERVICE_PORT", raising=False)
        mirror = build_bearer_mirror("dfe-ingest-bearer-tokens")

        assert mirror is not None
        with pytest.raises(SecretsError, match="KUBERNETES_SERVICE_HOST"):
            mirror.get_sync("http", provider="kubernetes")


class TestSetting:
    def test_the_environment_names_the_secret(self, monkeypatch):
        monkeypatch.setenv("DFE_INGEST_BEARER_SECRET", "dfe-ingest-bearer-tokens")

        assert _get_env_overrides()["ingest"]["bearer_secret"] == "dfe-ingest-bearer-tokens"

    def test_unset_by_default(self):
        assert IngestSettings().bearer_secret == ""

    @pytest.mark.parametrize("bad", ["Upper", "under_score", "-lead", "x" * 254])
    def test_a_name_the_api_server_would_refuse_is_refused(self, bad):
        with pytest.raises(ValueError, match="DFE_INGEST_BEARER_SECRET"):
            IngestSettings(bearer_secret=bad)
