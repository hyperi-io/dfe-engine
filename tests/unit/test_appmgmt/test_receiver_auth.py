#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_receiver_auth.py
#  Purpose:      A receiver listener authenticates while an ingest credential it accepts lists it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Each bearer listener's receiver auth, compiled from the credentials and the secrets backend.

A listener runs ``mode: none`` until an accepted credential lists it, then reads its tokens
from the secret the engine writes for it. The blocks are asserted as exact dicts per
backend, because a wrong one either leaves the listener open or refuses every sender.
"""

import json
from pathlib import Path

import pytest

from dfe_engine.appmgmt import appconfig, catalogue, instances, routing
from dfe_engine.orgs.ingest_credentials import IngestCredential
from dfe_engine.orgs.ingest_tokens import store_token, sync_bearer_tokens
from dfe_engine.orgs.models import Org
from dfe_engine.orgs.registry import OrgRegistry
from dfe_engine.orgs.seed import ensure_default_org
from dfe_engine.orgs.tenancy import Tenancy
from dfe_engine.secrets import build_secrets
from dfe_engine.services.registry import default_configs
from dfe_engine.services.templates import generate_template
from dfe_engine.settings import DFESettings, SecretsSettings
from dfe_engine.source.models import Source
from dfe_engine.yaml_utils import yaml_dump_string, yaml_load, yaml_load_string
from tests.support.config_schema import definitions, type_mismatches, unread_keys
from tests.support.producer_contract import DFE_RECEIVER, producer_file

from .conftest import FakeRegistry

RECEIVER = "dfe-receiver"

# Where the deployment mounts the file backend's ingest/bearer-tokens directory in the receiver.
FILE_MOUNT = "/var/run/secrets/dfe/ingest-bearer-tokens"

NONE = {"mode": "none"}

# A revoked credential's token stops within this plus the kubelet sync of a mounted Secret.
REFRESH = 60

# The receiver config section of each bearer listener, by the block the compile emits for it.
BLOCKS = {
    "server_auth": "server",
    "grpc_auth": "grpc",
    "otlp_auth": "otlp",
    "splunk_hec_auth": "splunk_hec",
    "prometheus_rw_auth": "prometheus_rw",
}

ORGS = (
    Org(name="default", org_ids=["default"]),
    Org(name="acme", org_ids=["acme-prod"]),
    Org(name="beta"),
)
AGENTS = IngestCredential(
    name="acme-agents", org="acme", orgs=["acme"], default_org="acme", listeners=["http", "otlp"]
)
HEC = IngestCredential(
    name="beta-hec", org="beta", orgs=["beta"], default_org="beta", listeners=["splunk_hec"]
)
TENANCY = Tenancy(orgs=ORGS, credentials=(AGENTS, HEC))
NO_CREDENTIAL = Tenancy(orgs=ORGS)

BACKENDS = {
    "aws": SecretsSettings(provider="aws", region="ap-southeast-2", name_prefix="dfe/dev/engine"),
    "openbao": SecretsSettings(provider="openbao", addr="https://bao:8200", mount="kv-dfe"),
    "vault": SecretsSettings(provider="vault", addr="https://vault:8200", mount="kv-dfe"),
    "file": SecretsSettings(provider="file", path="/app/config/.secrets"),
}

# Each listener's secret_source on each backend, with {listener} its transport label.
SOURCES = {
    "aws": "aws:dfe/dev/engine/ingest/bearer-tokens/{listener}",
    "openbao": "vault:kv-dfe/ingest/bearer-tokens/{listener}:value",
    "vault": "vault:kv-dfe/ingest/bearer-tokens/{listener}:value",
    "file": "file:/var/run/secrets/dfe/ingest-bearer-tokens/{listener}",
}


def _on(settings, backend: SecretsSettings):
    return settings.model_copy(update={"secrets": backend})


def _sources() -> FakeRegistry:
    doc = {
        "source": "filebeat",
        "state": "active",
        "match": {"field": "_json.app", "operator": "equals", "value": "filebeat"},
    }
    return FakeRegistry([Source.model_validate(doc)])


def _auth(settings, tenancy: Tenancy) -> dict:
    """The auth blocks of the receiver compile, by block name."""
    app = catalogue.descriptor(RECEIVER)
    compiled = routing.compile_for(app, _sources(), settings, tenancy=tenancy)
    return {name: block for name, block in compiled.items() if name in BLOCKS}


def _bearer(backend: str, listener: str) -> dict:
    source = SOURCES[backend].format(listener=listener)
    return {"mode": "bearer", "bearer": {"secret_source": source, "refresh_interval_secs": REFRESH}}


def _overlay() -> dict:
    return instances.initial_overlay(instances.instance_of(RECEIVER, "default"))


class TestManifest:
    def test_the_receiver_owns_each_bearer_listener_s_auth(self):
        paths = catalogue.descriptor(RECEIVER).routing_paths
        assert {name: paths.get(name) for name in BLOCKS} == {
            name: f"config.{section}.auth" for name, section in BLOCKS.items()
        }


class TestGolden:
    @pytest.mark.parametrize("backend", sorted(BACKENDS))
    def test_a_listener_a_credential_lists_reads_the_engine_s_secret(self, settings, backend):
        assert _auth(_on(settings, BACKENDS[backend]), TENANCY) == {
            "server_auth": _bearer(backend, "http"),
            "grpc_auth": NONE,
            "otlp_auth": _bearer(backend, "otlp"),
            "splunk_hec_auth": _bearer(backend, "splunk_hec"),
            "prometheus_rw_auth": NONE,
        }

    def test_the_aws_source_names_the_secret_whole(self, settings):
        auth = _auth(_on(settings, BACKENDS["aws"]), TENANCY)
        assert auth["server_auth"] == {
            "mode": "bearer",
            "bearer": {
                "secret_source": "aws:dfe/dev/engine/ingest/bearer-tokens/http",
                "refresh_interval_secs": REFRESH,
            },
        }

    @pytest.mark.parametrize("backend", sorted(BACKENDS))
    def test_with_no_credential_every_listener_runs_none(self, settings, backend):
        assert _auth(_on(settings, BACKENDS[backend]), NO_CREDENTIAL) == dict.fromkeys(BLOCKS, NONE)

    def test_an_openbao_mount_left_empty_names_the_path_as_the_engine_writes_it(self, settings):
        backend = SecretsSettings(provider="openbao", addr="https://bao:8200", mount="")
        auth = _auth(_on(settings, backend), TENANCY)
        assert auth["server_auth"]["bearer"] == {
            "secret_source": "vault:ingest/bearer-tokens/http:value",
            "refresh_interval_secs": REFRESH,
        }


class TestLiveCredentials:
    def test_disabling_the_only_credential_s_org_turns_its_listeners_back_to_none(self, settings):
        acme_off = ORGS[1].model_copy(update={"enabled": False})
        disabled = Tenancy(orgs=(ORGS[0], acme_off, ORGS[2]), credentials=TENANCY.credentials)

        assert _auth(settings, disabled) == {
            "server_auth": NONE,
            "grpc_auth": NONE,
            "otlp_auth": NONE,
            "splunk_hec_auth": _bearer("file", "splunk_hec"),
            "prometheus_rw_auth": NONE,
        }

    def test_a_credential_whose_org_is_gone_authenticates_nothing(self, settings):
        orphaned = Tenancy(orgs=ORGS[:2], credentials=(HEC,))
        assert _auth(settings, orphaned) == dict.fromkeys(BLOCKS, NONE)

    def test_a_synced_overlay_follows_the_org_switch(self, tmp_path, settings):
        registry = OrgRegistry(tmp_path / "orgs")
        ensure_default_org(registry)
        registry.create("acme")
        registry.ingest_credentials.put(AGENTS)
        app = catalogue.descriptor(RECEIVER)
        doc = _overlay()

        routing.sync(app, doc, _sources(), settings, tenancy=Tenancy.read(registry))
        assert doc["config"]["server"]["auth"] == _bearer("file", "http")

        registry.update("acme", enabled=False)
        assert routing.sync(app, doc, _sources(), settings, tenancy=Tenancy.read(registry)) is True
        assert doc["config"]["server"]["auth"] == NONE
        assert doc["config"]["otlp"]["auth"] == NONE

    def test_a_listener_opened_by_hand_is_drift_and_is_shut_again(self, settings):
        app = catalogue.descriptor(RECEIVER)
        doc = _overlay()
        routing.sync(app, doc, _sources(), settings, tenancy=TENANCY)
        doc["config"]["server"]["auth"] = NONE

        assert routing.status(app, doc, _sources(), settings, tenancy=TENANCY).drift is True
        assert routing.sync(app, doc, _sources(), settings, tenancy=TENANCY) is True
        assert doc["config"]["server"]["auth"] == _bearer("file", "http")


class TestUnreadableBackend:
    @pytest.mark.parametrize(
        ("provider", "reason"),
        [
            ("ansible_vault", "cannot decrypt"),
            ("gcp", "no provider"),
            ("azure", "no provider"),
            ("keychain", "not a scalo secrets provider"),
        ],
    )
    def test_a_credential_on_a_backend_the_receiver_cannot_read_is_refused(
        self, settings, provider, reason
    ):
        # gcp and azure are refused when settings load, so only a constructed one reaches here.
        fields = {**SecretsSettings().model_dump(), "provider": provider}
        backend = SecretsSettings.model_construct(**fields)

        with pytest.raises(ValueError, match=f"DFE_SECRETS_PROVIDER={provider}: .*{reason}"):
            _auth(_on(settings, backend), TENANCY)

    def test_with_no_credential_it_compiles_none_rather_than_refusing(self, settings):
        backend = SecretsSettings(provider="ansible_vault")
        assert _auth(_on(settings, backend), NO_CREDENTIAL) == dict.fromkeys(BLOCKS, NONE)


class TestFileMount:
    def test_the_file_source_is_the_engine_s_file_under_the_receiver_s_mount(
        self, tmp_path, settings
    ):
        # The deployment mounts <root>/ingest/bearer-tokens at FILE_MOUNT in the receiver.
        root = tmp_path / "secrets"
        backend = SecretsSettings(provider="file", path=str(root))
        registry = OrgRegistry(tmp_path / "orgs")
        ensure_default_org(registry)
        registry.create("acme")
        registry.ingest_credentials.put(AGENTS)
        secrets = build_secrets(backend)
        store_token(secrets, AGENTS, "token-a")
        sync_bearer_tokens(registry, secrets)

        auth = _auth(_on(settings, backend), Tenancy.read(registry))

        for block, listener in (("server_auth", "http"), ("otlp_auth", "otlp")):
            source = auth[block]["bearer"]["secret_source"]
            assert source == f"file:{FILE_MOUNT}/{listener}"
            written = root / "ingest" / "bearer-tokens" / listener
            assert written.read_text(encoding="utf-8") == "acme-agents:token-a\n"


@pytest.fixture(scope="module")
def receiver_schema() -> dict:
    """dfe-receiver's config schema at its pinned release, or the checkout named for it."""
    source = producer_file(DFE_RECEIVER, "docs/config-schema.json")
    schema = json.loads(source.text)
    # A reshaped schema must fail here, not pass every walk against nothing.
    assert schema.get("properties"), f"no top-level fields in {source.origin}"
    return schema


class TestProducerContract:
    @pytest.mark.parametrize("tenancy", [TENANCY, NO_CREDENTIAL], ids=["credentials", "none"])
    @pytest.mark.parametrize("backend", sorted(BACKENDS))
    def test_the_rendered_receiver_config_is_one_the_receiver_reads(
        self, receiver_schema, settings, backend, tenancy
    ):
        app = catalogue.descriptor(RECEIVER)
        doc = _overlay()
        routing.sync(app, doc, _sources(), _on(settings, BACKENDS[backend]), tenancy=tenancy)
        config = doc["config"]
        defs = definitions(receiver_schema)

        assert {config[section]["auth"]["mode"] for section in BLOCKS.values()} <= {
            "none",
            "bearer",
        }
        assert unread_keys(config, receiver_schema, defs) == []
        assert type_mismatches(config, receiver_schema, defs) == []


EVERY_LISTENER = IngestCredential(
    name="acme-all",
    org="acme",
    orgs=["acme"],
    default_org="acme",
    listeners=["http", "grpc", "otlp", "splunk_hec", "prometheus_rw"],
)
TENANCIES = {
    "some": TENANCY,
    "every": Tenancy(orgs=ORGS, credentials=(EVERY_LISTENER,)),
    "none": NO_CREDENTIAL,
}


class TestCompiledAuthOverTheTemplate:
    """A deployment that takes the production template as its receiver base still runs the compiled auth.

    The template is generated on one backend and the engine compiles on another, so a
    listener reading the compiled secret could not have got it from the template.
    """

    @pytest.mark.parametrize("tenancy", sorted(TENANCIES))
    @pytest.mark.parametrize("template_backend", ["aws", "openbao"])
    @pytest.mark.parametrize("engine_backend", ["aws", "openbao"])
    def test_every_listener_runs_the_compiled_block(
        self, crud, tmp_path, monkeypatch, engine_backend, template_backend, tenancy
    ):
        monkeypatch.setattr(
            "dfe_engine.settings._settings",
            DFESettings(env="dev", secrets=BACKENDS[template_backend]),
        )
        template = generate_template("receiver", "production")
        settings = DFESettings(
            env="dev",
            transport={"default": "bus"},
            secrets=BACKENDS[engine_backend],
            deployment={
                "profile": "docker-single",
                "target": "docker",
                "app_config_dir": str(tmp_path / "app-config"),
                "app_config_base_dir": str(tmp_path / "base"),
                "app_config_mount": "/etc/dfe/apps",
                "app_env_dir": str(tmp_path / "app-env"),
            },
        )
        base = Path(settings.deployment.app_config_base_dir)
        base.mkdir()
        (base / f"{RECEIVER}.yaml").write_text(yaml_dump_string(template), encoding="utf-8")
        app = catalogue.descriptor(RECEIVER)
        instance = instances.instance_of(RECEIVER, "default")
        doc = instances.initial_overlay(instance)
        routing.sync(app, doc, _sources(), settings, tenancy=TENANCIES[tenancy])
        crud.put(instances.HELMVARS_CLASS, instance.overlay_name, doc, "test", message="test")

        appconfig.render(crud, settings)

        rendered = yaml_load(Path(settings.deployment.app_config_dir) / RECEIVER / app.config_file)
        compiled = _auth(settings, TENANCIES[tenancy])
        for block, section in BLOCKS.items():
            runs = rendered[section]["auth"]
            assert runs["mode"] == compiled[block]["mode"], (section, runs)
            if compiled[block]["mode"] == "bearer":
                assert runs["bearer"]["secret_source"] == compiled[block]["bearer"]["secret_source"]
                assert runs["bearer"]["refresh_interval_secs"] == REFRESH


class TestSeededProductionConfig:
    @pytest.mark.parametrize("backend", ["aws", "openbao"])
    def test_the_seeded_production_config_holds_no_value_the_receiver_refuses(
        self, receiver_schema, backend
    ):
        engine = DFESettings(env="dev", secrets=BACKENDS[backend])
        seeded = yaml_load_string(default_configs(engine)["receiver-production.yaml"])

        assert type_mismatches(seeded, receiver_schema, definitions(receiver_schema)) == []
