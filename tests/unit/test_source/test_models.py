"""Tests for Source Pydantic models."""

import re
from typing import Any, get_args

import pytest
from pydantic import ValidationError
from scalo.api import ListQuery

from dfe_engine.appmgmt.catalogue import transform_engines
from dfe_engine.source.models import (
    DEFAULT_LANDING_LABEL,
    FetcherRoute,
    FetcherTopic,
    SchemaColumn,
    Source,
    SourceFetcher,
    SourceHeader,
    SourceMatch,
    SourceSchema,
    SourceSummaryObject,
    SourceSummaryPage,
    SourceTransform,
    SourceVersion,
    SourceVersionGetResponse,
    SourceView,
    SourceWriteRequest,
    apply_source_write_update,
    draft_build_version_to_invalidate,
    next_major_source_version,
    source_from_write,
    source_version_bump_required,
)
from dfe_engine.source.type_registry import TypeRegistry
from tests.support.present import present

MATCH = {"field": "f", "value": "v"}
OKTA_MAIN = {"source_type": "okta", "topic": "main"}
SIGMA_VIEW = {"standard": "sigma", "taxonomy": "windows"}
ELASTIC = {"engine": "elastic", "variant": "filebeat.cisco_asa.default"}


def _with_origin(body: dict[str, Any]) -> dict[str, Any]:
    """*body*, given a receiver match unless it already names an origin."""
    if {"match", "when", "fetcher", "external"} & body.keys():
        return body
    return {"match": MATCH, **body}


def _source(**body: Any) -> Source:
    return Source.model_validate({"source": "x", **_with_origin(body)})


def _write(**body: Any) -> SourceWriteRequest:
    return SourceWriteRequest.model_validate(_with_origin(body))


def _version(**body: Any) -> SourceVersion:
    return SourceVersion.model_validate({"date_time": "2026-01-01", **_with_origin(body)})


def _stored(
    versions: dict[str, dict[str, Any]], *, deployed: str | None = None, current: str | None = None
) -> Source:
    """A source with the version bodies in *versions*, whose current version is the last unless *current* is given."""
    return Source.model_validate(
        {
            "source": "src-a",
            "deployed_version": deployed,
            "current": current or list(versions)[-1],
            "versions": {
                vid: {"date_time": "2026-01-01", **_with_origin(body)}
                for vid, body in versions.items()
            },
        }
    )


def _at(source: Source, version: str, *path: str) -> Any:
    """Follow *path* as attributes from one stored version, e.g. ("schema_config", "ttl_days")."""
    value: Any = source.versions[version]
    for attr in path:
        value = getattr(value, attr)
    return value


@pytest.fixture
def registry() -> TypeRegistry:
    return TypeRegistry.default()


# ---------------------------------------------------------------------------
# SchemaColumn
# ---------------------------------------------------------------------------


class TestSchemaColumn:
    def test_minimal(self):
        col = SchemaColumn(name="user_name", type="string")
        assert col.attribute == []
        assert col.use_case is None
        assert col.ch_override is None

    def test_full(self):
        fields: dict[str, Any] = {
            "name": "source_ip",
            "type": "ip",
            "attribute": ["lowcardinality"],
            "use_case": "range",
            "default": "'::'",
            "order": 3,
            "expr": "@source: src_ip",
            "comment": "Source IP address",
        }
        col = SchemaColumn(**fields)
        assert {key: getattr(col, key) for key in fields} == fields

    @pytest.mark.parametrize(
        ("attribute", "expected"),
        [
            ("lowcardinality", ["lowcardinality"]),
            (["lowcardinality", "nullable"], ["lowcardinality", "nullable"]),
            (None, []),
        ],
    )
    def test_attribute_coercion(self, attribute, expected):
        assert SchemaColumn(name="x", type="string", attribute=attribute).attribute == expected

    @pytest.mark.parametrize(
        ("fields", "needles"),
        [
            ({"type": "string", "ch_override": "String"}, []),
            ({"type": "string", "use_case": "dimension"}, []),
            ({"type": "integer", "use_case": "word_search"}, ["word_search"]),
            ({"type": "json", "attribute": ["lowcardinality"]}, ["lowcardinality"]),
            ({"type": "string", "ch_override": "NotAType"}, ["NotAType"]),
            ({"type": "nonexistent"}, ["unknown primitive"]),
            (
                {"type": "json", "attribute": ["lowcardinality"], "use_case": "dimension"},
                ["dimension", "lowcardinality"],
            ),
        ],
    )
    def test_validate_against_registry(self, registry: TypeRegistry, fields, needles):
        errors = SchemaColumn(name="x", **fields).validate_against_registry(registry)

        assert len(errors) == len(needles)
        for error, needle in zip(errors, needles, strict=True):
            assert needle in error


# ---------------------------------------------------------------------------
# SourceHeader
# ---------------------------------------------------------------------------


class TestSourceHeader:
    def test_defaults(self):
        h = SourceHeader()
        assert h.type == "timeseries"
        assert h.version == "1.0.3"

        custom = SourceHeader(type="minimal", version="2.0.0")
        assert (custom.type, custom.version) == ("minimal", "2.0.0")

    def test_default_version_is_its_own_profile_s_current_marker(self):
        """An unpinned header lands on the revision its profile ships as ``current``.

        Reads the profile ``SourceHeader.type`` actually defaults to, not the
        minimal one: the two carry independent ``current`` markers.
        """
        from dfe_engine.schema.schema_loader import _resolve_profile_yaml_path
        from dfe_engine.yaml_utils import yaml_load

        default = SourceHeader()
        path = _resolve_profile_yaml_path(f"common-header/{default.type}", None)
        assert path.is_file(), f"no profile on disk for the default header type {default.type!r}"

        assert str(yaml_load(path).get("current") or "") == default.version


# ---------------------------------------------------------------------------
# SourceMatch
# ---------------------------------------------------------------------------


class TestSourceMatch:
    def test_creation(self):
        m = SourceMatch(field="tags.collector.type", value="filebeat")
        assert m.operator == "equals"

    def test_exists_operator_allows_empty_value(self):
        m = SourceMatch(field="_json.tags.type", operator="exists", value="")
        assert m.operator == "exists"
        assert m.value == ""

    def test_value_required_for_equals(self):
        with pytest.raises(ValueError, match=r"match\.value is required"):
            SourceMatch(field="_json.f", operator="includes", value="  ")

    def test_always_needs_no_operand_and_every_other_operator_does(self):
        assert SourceMatch(field="_source", operator="always").value == ""

        with pytest.raises(ValueError, match=r"match\.value is required"):
            SourceMatch(field="_source", operator="equals")


# ---------------------------------------------------------------------------
# SourceSchema
# ---------------------------------------------------------------------------


class TestSourceSchema:
    def test_minimal(self):
        s = SourceSchema(ttl_days=90)
        assert s.engine == ""  # empty = inherit the deployment default at DDL time
        assert s.meta_schema is None

    def test_full(self):
        s = SourceSchema(
            meta_schema="logs_beats_filebeat",
            meta_schema_version="1.0.0",
            derived_schema="filebeat/derived",
            additional_fields="filebeat/add",
            ttl_days=90,
            engine="ReplacingMergeTree",
        )
        assert s.meta_schema == "logs_beats_filebeat"
        assert s.engine == "ReplacingMergeTree"

    def test_zero_ttl_is_accepted_and_negative_refused(self):
        assert SourceSchema(ttl_days=0).ttl_days == 0
        with pytest.raises(ValidationError):
            SourceSchema(ttl_days=-1)

    def test_parameterised_engine(self):
        # A parameterised variant is accepted - the registry gates the variant,
        # the params (a version column here) are the caller's.
        s = SourceSchema(engine="ReplacingMergeTree(_timestamp_load)")
        assert s.engine == "ReplacingMergeTree(_timestamp_load)"

    @pytest.mark.parametrize("engine", ["InvalidEngine", "ReplicatedMergeTree"])
    def test_invalid_engine(self, engine):
        # The Replicated prefix is added at DDL time, never declared in source config.
        with pytest.raises(ValueError, match="Invalid engine"):
            SourceSchema(engine=engine)


# ---------------------------------------------------------------------------
# SourceTransform
# ---------------------------------------------------------------------------


class TestSourceTransform:
    def test_defaults(self):
        t = SourceTransform(engine="vector", config_file="/etc/vector/fb.yaml")
        assert t.env == {}
        assert t.files == []

    def test_every_catalogued_transform_is_accepted(self):
        engines = transform_engines()
        assert engines
        for engine in engines:
            assert SourceTransform(engine=engine).engine == engine

    def test_invalid_engine_names_the_catalogue(self):
        with pytest.raises(ValueError, match="Invalid transform engine") as exc:
            SourceTransform(engine="spark")
        # The catalogue's engines, plus the customer's own pipeline.
        assert ", ".join(sorted(transform_engines() | {"external"})) in str(exc.value)

    @pytest.mark.parametrize(
        ("id_", "fields", "mode", "detect"),
        [
            ("unset_leaves_the_app_default", {}, None, None),
            (
                "agent_names_its_variant",
                {"mode": "agent", "variant": "filebeat.x.default"},
                "agent",
                None,
            ),
            ("agentless_with_no_variant", {"mode": "agentless"}, "agentless", None),
            (
                "agentless_defaults_detect",
                {"mode": "agentless", "detect": {}},
                "agentless",
                {"unmatched": "passthrough", "fallback": "", "candidates": []},
            ),
            (
                "fallback_names_its_source",
                {
                    "mode": "agentless",
                    "detect": {"unmatched": "fallback", "fallback": " filebeat.cisco_asa.default "},
                },
                "agentless",
                {
                    "unmatched": "fallback",
                    "fallback": "filebeat.cisco_asa.default",
                    "candidates": [],
                },
            ),
            (
                "candidates_keep_their_order",
                {
                    "mode": "agentless",
                    "detect": {
                        "unmatched": "stop",
                        "candidates": ["filebeat.cisco_ftd.default", "filebeat.cisco_asa.default"],
                    },
                },
                "agentless",
                {
                    "unmatched": "stop",
                    "fallback": "",
                    "candidates": ["filebeat.cisco_ftd.default", "filebeat.cisco_asa.default"],
                },
            ),
        ],
    )
    def test_elastic_mode_and_detect_are_accepted(self, id_, fields, mode, detect):
        t = SourceTransform(engine="elastic", **fields)

        assert t.mode == mode
        assert (t.detect.model_dump() if t.detect else None) == detect, id_

    @pytest.mark.parametrize(
        ("id_", "fields", "refusal"),
        [
            ("an_unknown_mode", {"mode": "hybrid"}, "mode"),
            (
                "an_unknown_unmatched",
                {"mode": "agentless", "detect": {"unmatched": "drop"}},
                "unmatched",
            ),
            (
                "fallback_without_the_fallback_source",
                {"mode": "agentless", "detect": {"unmatched": "fallback"}},
                "fallback is required when unmatched is fallback",
            ),
            (
                "fallback_beside_passthrough",
                {"mode": "agentless", "detect": {"fallback": "filebeat.cisco_asa.default"}},
                "fallback is set but unmatched is 'passthrough'",
            ),
            (
                "fallback_beside_stop",
                {
                    "mode": "agentless",
                    "detect": {"unmatched": "stop", "fallback": "filebeat.cisco_asa.default"},
                },
                "fallback is set but unmatched is 'stop'",
            ),
            (
                "a_blank_candidate",
                {
                    "mode": "agentless",
                    "detect": {"candidates": ["filebeat.cisco_asa.default", " "]},
                },
                "cannot be blank",
            ),
            (
                "a_repeated_candidate",
                {"mode": "agentless", "detect": {"candidates": ["a.b.default", "a.b.default"]}},
                "listed twice",
            ),
            (
                "detect_without_agentless",
                {"detect": {"unmatched": "stop"}},
                "read only when transform.mode is agentless",
            ),
            (
                "detect_in_agent_mode",
                {"mode": "agent", "detect": {"unmatched": "stop"}},
                "read only when transform.mode is agentless",
            ),
            (
                "agentless_beside_a_variant",
                {"mode": "agentless", "variant": "filebeat.cisco_asa.default"},
                "takes no variant",
            ),
        ],
    )
    def test_elastic_mode_and_detect_refuse(self, id_, fields, refusal):
        with pytest.raises(ValueError, match=refusal):
            SourceTransform(engine="elastic", **fields)

    @pytest.mark.parametrize(
        ("engine", "fields", "refusal"),
        [
            ("vrl", {"mode": "agentless"}, "no agent or agentless mode"),
            ("vector", {"mode": "agent"}, "no agent or agentless mode"),
            ("vrl", {"mode": "agentless", "detect": {}}, "no agent or agentless mode"),
        ],
    )
    def test_an_app_with_no_mode_path_refuses_one(self, engine, fields, refusal):
        with pytest.raises(ValueError, match=refusal):
            SourceTransform(engine=engine, **fields)


# ---------------------------------------------------------------------------
# SourceFetcher
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SourceFetcher
# ---------------------------------------------------------------------------


class TestSourceFetcher:
    def test_defaults(self):
        f = SourceFetcher(source_type="crowdstrike")
        assert f.topic == "own"
        assert f.config == {}

    @pytest.mark.parametrize(
        ("topic", "source", "label"), [("own", "edr", "edr"), ("main", "okta-audit", "main")]
    )
    def test_main_topic_lands_on_the_shared_landing(self, topic, source, label):
        assert SourceFetcher(source_type="okta", topic=topic).landing_label(source) == label

    def test_the_topic_literal_still_carries_the_landing_label(self):
        # FetcherTopic cannot be built from a constant, so it restates the label.
        assert DEFAULT_LANDING_LABEL in get_args(FetcherTopic)

    def test_unknown_source_type_is_refused(self):
        # The manifest lists the families the fetcher ships. Anything else has
        # no stanza the fetcher would read.
        with pytest.raises(ValueError, match="Unknown fetcher source_type"):
            SourceFetcher(source_type="http_json")

    def test_engine_owned_keys_are_refused(self):
        with pytest.raises(ValueError, match="may not set"):
            SourceFetcher(source_type="okta", config={"enabled": True, "topic": "x"})

    @pytest.mark.parametrize(
        "config",
        [
            {"token": "abc"},
            {"connections": [{"id": "a", "client_secret": "x"}]},
            {"credential_secret": "not-a-reference"},
            {"backends": [{"account_key": "literal"}]},
        ],
    )
    def test_a_literal_credential_is_refused(self, config):
        # The source YAML is committed to git, so credentials travel as references.
        with pytest.raises(ValueError, match="literal credential"):
            SourceFetcher(source_type="okta", config=config)

    @pytest.mark.parametrize(
        ("config", "where"),
        [
            ({"apiKey": "k-4460"}, "apiKey"),
            ({"x-api-key": "k-4461"}, "x-api-key"),
            ({"privateKey": "-----BEGIN PRIVATE KEY-----"}, "privateKey"),
            ({"accessKeyId": "AKIA4462"}, "accessKeyId"),
            ({"backends": [{"accountKey": "ak-4463"}]}, "backends[0].accountKey"),
            ({"connection_string": "AccountName=a;AccountKey=ak-4464"}, "connection_string"),
            ({"auth": {"credentials_json": '{"secret_access_key": "x"}'}}, "auth.credentials_json"),
            ({"auth": {"key": "k-4465"}}, "auth.key"),
            ({"auth": {"credentials": {"pair_a": "pa-4466"}}}, "auth.credentials.pair_a"),
            ({"auth": {"service_account_key": '{"private_key": "x"}'}}, "auth.service_account_key"),
            (
                {"auth": {"oauth2_client_credentials": {"client_secret": "cs-4467"}}},
                "auth.oauth2_client_credentials.client_secret",
            ),
        ],
    )
    def test_a_literal_credential_in_any_spelling_is_refused(self, config, where):
        with pytest.raises(ValueError, match=rf"fetcher\.config\.{re.escape(where)} holds"):
            SourceFetcher(source_type="okta", config=config)

    @pytest.mark.parametrize(
        "config",
        [
            {"admin_email": "admin@corp.example"},
            {"service_account_key": "/etc/gcp/sa-key.json"},
            {"connections": [{"id": "a", "service_account_key": "/etc/gcp/a.json"}]},
            {"auth": {"service_account_key_file": "/etc/gcp/sa.json", "private_key_id": "kid-1"}},
            {
                "auth": {
                    "oauth2_client_credentials": {
                        "token_url": "https://idp.example/token",
                        "scope": "logs.read",
                    }
                }
            },
            {
                "auth": {
                    "session_login": {
                        "password_field": "password",
                        "token_at": "/token",
                        "token_header": "X-Session",
                    }
                }
            },
            {"auth": {"credentials": [{"header": "X-Key", "value": "{{ credentials.pair_a }}"}]}},
            {"auth": {"credentials": {"pair_a": "vault:kv/dfe/pair:a"}}},
            {"auth": {"password_env": "FETCHER_PASSWORD", "api_token_env": "FETCHER_TOKEN"}},
        ],
    )
    def test_a_setting_beside_a_credential_word_is_accepted(self, config):
        assert SourceFetcher(source_type="okta", config=config).config == config

    def test_credential_references_and_paths_are_accepted(self):
        f = SourceFetcher(
            source_type="google_workspace",
            config={
                "credential_secret": "vault:secret/dfe/gw:sa_key",
                "service_account_key": "/etc/workspace/sa-key.json",
                "admin_email": "env:GW_ADMIN",
            },
        )
        assert f.config["credential_secret"].startswith("vault:")

    @pytest.mark.parametrize(
        "reference",
        [
            "vault:kv/data/dfe-test/aws:secret_access_key",
            "bao:kv/dfe-test/aws:secret_access_key",
            "openbao:kv/dfe-test/aws:secret_access_key",
            "env:AWS_SECRET_ACCESS_KEY",
            "file:/run/secrets/aws-key",
        ],
    )
    def test_every_prefix_the_fetcher_resolves_is_accepted(self, reference):
        """The engine's list is dfe-fetcher's `RESOLVED_PREFIXES` (#490)."""
        f = SourceFetcher(source_type="aws", config={"secret_access_key": reference})
        assert f.config["secret_access_key"] == reference

    def test_a_prefix_the_fetcher_cannot_resolve_is_still_refused(self):
        """`aws:` is in the resolver's vocabulary with no provider behind it, so a
        reference using it would reach the consumer as its own literal text.
        """
        with pytest.raises(ValueError, match="literal credential"):
            SourceFetcher(source_type="aws", config={"secret_access_key": "aws:some/path"})


class TestSourceOrigin:
    def test_an_operator_source_needs_exactly_one_origin(self):
        with pytest.raises(ValueError, match="exactly one"):
            Source.model_validate({"source": "x"})
        with pytest.raises(ValueError, match="not more"):
            Source.model_validate(
                {"source": "x", "match": MATCH, "fetcher": {"source_type": "okta"}}
            )

    def test_a_core_source_may_have_no_origin_at_all(self):
        s = Source.model_validate({"source": "main", "resource_type": "core"})

        assert s.origin is None
        assert s.match is None
        assert s.fetcher is None
        assert s.model_dump(mode="json")["origin"] is None

    def test_a_core_source_still_cannot_declare_both(self):
        with pytest.raises(ValueError, match="not more"):
            Source.model_validate(
                {
                    "source": "main",
                    "resource_type": "core",
                    "match": MATCH,
                    "fetcher": {"source_type": "okta"},
                }
            )

    def test_a_match_rule_makes_a_receiver_source(self):
        s = _source(source="syslog")
        assert s.origin == "receiver"
        assert s.fetcher is None
        assert s.landing_label() == "syslog"
        assert s.table_topic_type == "main"
        assert s.model_dump(mode="json")["origin"] == "receiver"

    def test_a_fetcher_makes_a_fetcher_source(self):
        s = _source(source="okta-audit", fetcher=OKTA_MAIN)
        assert s.origin == "fetcher"
        assert s.match is None
        assert s.landing_label() == "main"
        assert s.table_topic_type == "main"
        assert "match" not in s.to_yaml_dict()["versions"]["1.0.0"]

    @pytest.mark.parametrize(
        ("schema", "topic_type"),
        [
            ({"meta_schema": "meta/aws_cloudtrail", "meta_schema_version": "1.0.0"}, "own"),
            ({}, "main"),
        ],
        ids=["meta-schema-pinned", "no-meta-schema"],
    )
    def test_a_meta_schema_pin_means_the_source_owns_its_table(self, schema, topic_type):
        assert _source(schema=schema).table_topic_type == topic_type

    def test_the_landing_source_is_the_main_table(self):
        s = Source.model_validate({"source": "main", "resource_type": "core"})
        assert s.table_topic_type == "main"

    def test_table_topic_type_follows_the_current_version_not_the_deployed_one(self):
        """An undeployed current without a meta schema shares main, even if deployed owns a table."""
        s = _stored(
            {
                "1.0.0": {"schema": {"meta_schema": "meta/azure", "meta_schema_version": "1.0.0"}},
                "2.0.0": {"schema": {}},
            },
            deployed="1.0.0",
        )
        assert s.version("1.0.0").effective_schema().meta_schema == "meta/azure"
        assert s.version("2.0.0").effective_schema().meta_schema is None
        assert s.table_topic_type == "main"

        # Flip current back to the deployed pin: own table again.
        s = s.model_copy(update={"current": "1.0.0"})
        assert s.table_topic_type == "own"

    def test_the_write_body_needs_exactly_one_origin(self):
        with pytest.raises(ValueError, match="exactly one"):
            SourceWriteRequest(source="x")
        body = SourceWriteRequest(source="x", fetcher=SourceFetcher(source_type="pypi"))
        assert body.to_version_snapshot().origin == "fetcher"

    @pytest.mark.parametrize(
        ("existing", "body", "refusal"),
        [
            (
                {"match": MATCH},
                {"fetcher": {"source_type": "okta", "topic": "own"}},
                "Cannot change source origin",
            ),
            (
                {"fetcher": {"source_type": "okta", "topic": "own"}},
                {"match": MATCH},
                "Cannot change source origin",
            ),
            (
                {
                    "match": MATCH,
                    "schema": {"meta_schema": "meta/aws", "meta_schema_version": "1.0.0"},
                },
                {"match": MATCH, "schema": {"engine": "MergeTree"}},
                "Cannot remove the meta schema",
            ),
        ],
        ids=["receiver-to-fetcher", "fetcher-to-receiver", "clear-meta-schema"],
    )
    def test_an_update_cannot_swap_the_origin_or_drop_the_meta_schema(
        self, existing, body, refusal
    ):
        with pytest.raises(ValueError, match=refusal):
            apply_source_write_update(
                Source.model_validate({"source": "x", **existing}),
                SourceWriteRequest.model_validate(body),
            )

    @pytest.mark.parametrize(
        ("before", "after", "pinned"),
        [
            (
                {"meta_schema": "meta/aws", "meta_schema_version": "1.0.0"},
                {"meta_schema": "meta/aws_cloudtrail", "meta_schema_version": "2.0.0"},
                "meta/aws_cloudtrail",
            ),
            ({}, {"meta_schema": "meta/azure", "meta_schema_version": "1.0.0"}, "meta/azure"),
        ],
        ids=["change-the-pin", "gain-a-pin"],
    )
    def test_an_update_may_change_or_gain_a_meta_schema(self, before, after, pinned):
        updated = apply_source_write_update(_source(schema=before), _write(schema=after))

        assert present(updated.versions["1.0.0"].schema_config).meta_schema == pinned
        assert updated.table_topic_type == "own"


# ---------------------------------------------------------------------------
# SourceView
# ---------------------------------------------------------------------------


class TestSourceView:
    def test_defaults(self):
        v = SourceView(standard="sigma")
        assert v.field_map is None
        assert v.custom_mappings == {}
        assert v.taxonomy is None
        assert v.category is None
        assert v.service is None

    def test_with_mappings(self):
        v = SourceView(
            standard="ecs",
            taxonomy="windows",
            field_map="ecs/filebeat",
            custom_mappings={"CommandLine": "command_line"},
        )
        assert v.taxonomy == "windows"
        assert v.field_map == "ecs/filebeat"
        assert v.custom_mappings["CommandLine"] == "command_line"

    def test_standard_forced_lowercase(self):
        assert SourceView(standard="ECS").standard == "ecs"

    def test_unknown_standard_rejected(self):
        with pytest.raises(ValueError, match="Unknown view standard"):
            SourceView(standard="splunk_cim_v9")


# ---------------------------------------------------------------------------
# Source - Top-Level
# ---------------------------------------------------------------------------


class TestSource:
    def test_defaults(self):
        s = _source(source="crowdstrike-edr")
        assert s.display_name == "Crowdstrike Edr"
        assert s.enabled is True
        assert s.state == "active"
        assert s.version().header is None
        assert s.header == SourceHeader()
        assert s.schema_config.engine == ""  # empty = inherit deployment default
        assert s.views == []
        assert s.view_for("sigma") is None
        assert s.transport is None
        assert s.archive is False
        assert s.topic_land == "crowdstrike-edr_land"
        assert s.topic_load is None
        assert s.table_name == "crowdstrike-edr"

    def test_full(self):
        s = Source.model_validate(
            {
                "source": "filebeat",
                "display_name": "Elastic Filebeat",
                "description": "Filebeat log collector",
                "enabled": True,
                "header": {"type": "timeseries", "version": "1.0.0"},
                "match": {"field": "tags.collector.type", "value": "filebeat"},
                "schema": {
                    "meta_schema": "logs_beats_filebeat",
                    "ttl_days": 90,
                    "engine": "MergeTree",
                },
                "transform": {"engine": "vector", "config_file": "/etc/vector/filebeat.yaml"},
                "views": [
                    {
                        "standard": "sigma",
                        "taxonomy": "linux",
                        "custom_mappings": {"User": "user_name"},
                    }
                ],
            }
        )
        assert s.display_name == "Elastic Filebeat"
        assert s.schema_config.meta_schema == "logs_beats_filebeat"
        assert present(s.transform).engine == "vector"
        assert present(s.view_for("sigma")).taxonomy == "linux"
        assert s.topic_load == "filebeat_load"

    def test_display_name_explicit(self):
        s = _source(source="crowdstrike-edr", display_name="CrowdStrike EDR")
        assert s.display_name == "CrowdStrike EDR"

    @pytest.mark.parametrize(
        ("lifecycle", "state"),
        [
            ({"enabled": False}, "disabled"),
            ({"enabled": True}, "active"),
            ({"enabled": True, "state": "dormant"}, "dormant"),
            ({"state": "dormant"}, "dormant"),
        ],
        ids=["legacy-disabled", "legacy-enabled", "state-wins", "state-only"],
    )
    def test_state_and_the_compat_enabled_flag(self, lifecycle, state):
        s = _source(**lifecycle)

        assert s.state == state
        assert s.enabled is (state == "active")
        dumped = s.model_dump(mode="json")
        assert (dumped["state"], dumped["enabled"]) == (state, state == "active")
        yaml_dict = s.to_yaml_dict()
        assert "enabled" not in yaml_dict
        assert Source.model_validate(yaml_dict).state == state

    def test_views_set(self):
        s = _source(views=[SourceView(standard="sigma"), SourceView(standard="ecs")])

        assert [v.standard for v in s.views] == ["sigma", "ecs"]
        assert s.to_yaml_dict()["versions"]["1.0.0"]["views"] == [
            {"standard": "sigma"},
            {"standard": "ecs"},
        ]


# ---------------------------------------------------------------------------
# Source - Naming Validation
# ---------------------------------------------------------------------------


class TestSourceNaming:
    """A source name has to survive as the DNS-1123 label an instance is named for."""

    @pytest.mark.parametrize(
        "name", ["filebeat", "syslog", "crowdstrike-edr", "a", "x123-456", "a" * 40]
    )
    def test_valid_names_are_legal_instance_names(self, name):
        from dfe_engine.appmgmt.instances import validate_instance

        assert _source(source=name).source == name
        validate_instance(name)

    @pytest.mark.parametrize(
        ("name", "refusal"),
        [
            ("123abc", "DNS-1123 label"),
            ("FileBeat", "DNS-1123 label"),
            ("filebeat-", "DNS-1123 label"),
            ("file beat", "DNS-1123 label"),
            ("_test", "DNS-1123 label"),
            ("a" * 41, "exceeds max length"),
            ("", "DNS-1123 label"),
        ],
        ids=[
            "digit-first",
            "uppercase",
            "trailing-hyphen",
            "space",
            "underscore-first",
            "too-long",
            "empty",
        ],
    )
    def test_invalid_names_are_refused(self, name, refusal):
        with pytest.raises(ValueError, match=refusal):
            Source.model_validate({"source": name})

    def test_underscores_are_refused_and_the_error_says_why(self):
        """The message has to name the constraint, not just quote the regex."""
        with pytest.raises(ValueError) as caught:
            _source(source="crowdstrike_edr")
        message = str(caught.value)
        assert "DNS-1123 label" in message
        assert "use '-' instead of '_'" in message
        # It says what breaks, so the reader knows why the rule exists.
        assert "instance" in message


# ---------------------------------------------------------------------------
# Source - YAML Serialisation
# ---------------------------------------------------------------------------


class TestSourceYaml:
    def test_round_trip(self):
        data = {
            "source": "filebeat",
            "display_name": "Filebeat",
            "description": "A test",
            "enabled": False,
            "match": {"field": "tags.collector.type", "value": "filebeat"},
            "header": {"type": "minimal", "version": "2.0.0"},
            "schema": {"meta_schema": "test_meta", "ttl_days": 90, "engine": "SummingMergeTree"},
        }
        s = Source.model_validate(data)
        yaml_dict = s.to_yaml_dict()

        assert yaml_dict["versions"]["1.0.0"]["match"]["field"] == "tags.collector.type"
        assert yaml_dict["versions"]["1.0.0"]["schema"]["ttl_days"] == 90
        assert "schema_config" not in yaml_dict
        assert "deployed_version" not in yaml_dict
        assert "current" in yaml_dict
        assert Source.model_validate(yaml_dict).to_yaml_dict() == yaml_dict

    def test_excludes_none(self):
        yaml_dict = _source().to_yaml_dict()
        for key in (
            "match",
            "transform",
            "deployed_version",
            "fetcher",
            "views",
            "description",
            "from_catalogue_item",
            "from_catalogue_intake",
        ):
            assert key not in yaml_dict

    def test_catalogue_provenance_is_stored_and_kept_across_an_edit(self):
        existing = _source(source="okta", from_catalogue_item="okta", from_catalogue_intake="beats")
        dumped = existing.to_yaml_dict()
        assert dumped["from_catalogue_item"] == "okta"
        assert dumped["from_catalogue_intake"] == "beats"
        assert Source.model_validate(dumped).from_catalogue_intake == "beats"

        updated = apply_source_write_update(existing, _write(description="doc-only edit"))
        assert updated.from_catalogue_item == "okta"
        assert updated.from_catalogue_intake == "beats"
        assert updated.description == "doc-only edit"


class TestSourceVersion:
    def test_yaml_omits_empty_containers_and_the_derived_origin(self):
        ver = _version(views=[], date_time="2026-06-10")
        out = ver.to_yaml_dict()
        assert "views" not in out
        assert out["date_time"] == "2026-06-10"

        with_view = _version(views=[SourceView(standard="sigma")]).to_yaml_dict()
        assert with_view["views"] == [{"standard": "sigma"}]

    @pytest.mark.parametrize(
        ("body", "origin"),
        [
            ({"match": MATCH}, "receiver"),
            ({"fetcher": {"source_type": "okta"}}, "fetcher"),
            ({"match": None}, None),
        ],
    )
    def test_origin_is_on_the_api_dump_but_not_in_yaml(self, body, origin):
        """origin is derived for responses. Writing it would duplicate match/fetcher."""
        ver = SourceVersion.model_validate({"date_time": "2026-06-10", **body})

        assert ver.model_dump(mode="json")["origin"] == origin
        assert "origin" not in ver.to_yaml_dict()

    def test_view_for(self):
        ver = _version(views=[SourceView(standard="sigma", taxonomy="windows")])
        assert present(ver.view_for("sigma")).taxonomy == "windows"
        assert ver.view_for("ecs") is None

    def test_duplicate_view_standards_rejected(self):
        """Duplicate standards would let view_for (first wins) and the DDL
        overlay (last wins) silently disagree - refused outright."""
        with pytest.raises(ValueError, match="duplicate view"):
            _version(
                views=[
                    SourceView(standard="sigma", taxonomy="windows"),
                    SourceView(standard="sigma", taxonomy="linux"),
                ]
            )


class TestSourceWriteRequest:
    @pytest.mark.parametrize(
        ("body", "refusal"),
        [
            ({"versions": {"1.0.0": {}}}, "not allowed"),
            ({"deployed_version": "1.0.0"}, "not allowed"),
            *(
                ({removed: {"taxonomy": "windows"}}, r"removed in 2\.2")
                for removed in ("sigma", "mapping_standards", "field_mappings")
            ),
        ],
    )
    def test_refuses_version_tree_and_removed_2_1_keys(self, body, refusal):
        """Pre-2.2 mapping keys fail LOUDLY - extra=ignore would otherwise 200
        while the client's mapping config silently vanished."""
        with pytest.raises(ValueError, match=refusal):
            _write(source="x", **body)

    @pytest.mark.parametrize(
        ("lifecycle", "state"),
        [
            ({}, "dormant"),
            ({"enabled": False}, "disabled"),
            ({"enabled": True}, "active"),
            ({"enabled": True, "state": "dormant"}, "dormant"),
        ],
        ids=["neither-keeps-the-current-state", "enabled-false", "enabled-true", "state-wins"],
    )
    def test_effective_state(self, lifecycle, state):
        assert _write(**lifecycle).effective_state("dormant") == state

    def test_apply_write_update_without_state_or_enabled_keeps_dormant(self):
        """A PUT that sends NEITHER state nor enabled must not re-activate a
        dormant source (effective_state keeps the current tri-state)."""
        updated = apply_source_write_update(
            _source(state="dormant"), _write(description="doc-only edit")
        )
        assert updated.state == "dormant"
        assert updated.description == "doc-only edit"

    def test_create_uses_1_0_0_not_header_profile_version(self):
        write = _write(
            source="profile-pin",
            header={"type": "common-header/minimal", "version": "1.1.0"},
            schema={"engine": "MergeTree"},
        )
        src = source_from_write(write, source_name="profile-pin")
        assert "1.0.0" in src.versions
        assert present(src.versions["1.0.0"].header).version == "1.1.0"
        assert src.current == "1.0.0"
        assert src.deployed_version is None

    def test_create_without_header_leaves_header_unset(self):
        src = source_from_write(_write(source="no-header"), source_name="no-header")
        ver = src.versions["1.0.0"]
        assert ver.header is None
        assert "header" not in ver.to_yaml_dict()

    def test_next_major_source_version(self):
        assert next_major_source_version({}) == "1.0.0"
        assert next_major_source_version({"1.0.0": {}}) == "2.0.0"
        assert next_major_source_version({"1.0.0": {}, "2.1.0": {}}) == "3.0.0"

    def test_next_major_source_version_rejects_invalid_ids(self):
        with pytest.raises(ValueError, match="not semver"):
            next_major_source_version({"v1": {}})

    def test_next_major_semver_invalid(self):
        from dfe_engine.source.models import _next_major_semver

        with pytest.raises(ValueError, match="not semver"):
            _next_major_semver("not-a-version")

    def test_apply_write_update_refuses_overwrite(self, monkeypatch):
        existing = _stored({"1.0.0": {}, "2.0.0": {}}, deployed="1.0.0", current="1.0.0")
        monkeypatch.setattr(
            "dfe_engine.source.models.next_major_source_version", lambda _versions: "2.0.0"
        )
        with pytest.raises(ValueError, match="Refusing to overwrite"):
            apply_source_write_update(
                existing, _write(schema={"engine": "MergeTree", "meta_schema_version": "2.0.0"})
            )

    @pytest.mark.parametrize(
        ("stored", "deployed", "body", "current", "ids", "checks"),
        [
            pytest.param(
                {"1.0.0": {"schema": {"ttl_days": 90, "meta_schema_version": "1.0.0"}}},
                "1.0.0",
                {
                    "description": "rev 2",
                    "schema": {"ttl_days": 30, "meta_schema_version": "2.0.0"},
                },
                "2.0.0",
                {"1.0.0", "2.0.0"},
                [
                    (("1.0.0", "schema_config", "ttl_days"), 90),
                    (("2.0.0", "schema_config", "ttl_days"), 30),
                    (("2.0.0", "schema_config", "meta_schema_version"), "2.0.0"),
                ],
                id="schema-pin-bumps-a-deployed-version",
            ),
            pytest.param(
                {"1.0.0": {"schema": {"meta_schema_version": "1.0.0"}}},
                None,
                {"schema": {"meta_schema_version": "2.0.0"}},
                "1.0.0",
                {"1.0.0"},
                [(("1.0.0", "schema_config", "meta_schema_version"), "2.0.0")],
                id="an-undeployed-version-is-edited-in-place",
            ),
            pytest.param(
                {"1.0.0": {"schema": {"ttl_days": 90}}},
                "1.0.0",
                {"match": {"field": "g", "value": "w"}, "schema": {"ttl_days": 90}},
                "1.0.0",
                {"1.0.0"},
                [(("1.0.0", "match", "field"), "g")],
                id="a-match-change-does-not-shape-the-table",
            ),
            pytest.param(
                {"1.0.0": {"schema": {"ttl_days": 90}}},
                "1.0.0",
                {"schema": {"ttl_days": 30}},
                "2.0.0",
                {"1.0.0", "2.0.0"},
                [
                    (("1.0.0", "schema_config", "ttl_days"), 90),
                    (("2.0.0", "schema_config", "ttl_days"), 30),
                ],
                id="ttl-alone-bumps-a-deployed-version",
            ),
            pytest.param(
                {"1.0.0": {"schema": {"engine": "MergeTree"}}},
                "1.0.0",
                {"schema": {"engine": "ReplacingMergeTree"}},
                "2.0.0",
                {"1.0.0", "2.0.0"},
                [(("2.0.0", "schema_config", "engine"), "ReplacingMergeTree")],
                id="engine-bumps-a-deployed-version",
            ),
            pytest.param(
                {"1.0.0": {"header": {"type": "timeseries", "version": "1.0.0"}, "schema": {}}},
                "1.0.0",
                {"header": {"type": "timeseries", "version": "1.0.1"}, "schema": {}},
                "2.0.0",
                {"1.0.0", "2.0.0"},
                [(("2.0.0", "header", "version"), "1.0.1")],
                id="header-bumps-a-deployed-version",
            ),
            pytest.param(
                {
                    "1.0.0": {"schema": {}},
                    "2.0.0": {
                        "schema": {
                            "meta_schema": "meta/aws/cloudwatch",
                            "meta_schema_version": "1.0.0",
                        }
                    },
                },
                "2.0.0",
                {"schema": {"meta_schema": "meta/aws/guardduty", "meta_schema_version": "1.0.0"}},
                "3.0.0",
                {"1.0.0", "2.0.0", "3.0.0"},
                [
                    (("2.0.0", "schema_config", "meta_schema"), "meta/aws/cloudwatch"),
                    (("3.0.0", "schema_config", "meta_schema"), "meta/aws/guardduty"),
                ],
                id="meta-schema-path-bumps-a-deployed-version",
            ),
            pytest.param(
                {
                    "1.0.0": {
                        "schema": {
                            "meta_schema": "meta/aws/cloudwatch",
                            "meta_schema_version": "1.0.0",
                        }
                    },
                    "2.0.0": {
                        "schema": {
                            "meta_schema": "meta/aws/cloudwatch",
                            "meta_schema_version": "1.0.0",
                        }
                    },
                },
                "1.0.0",
                {"schema": {"meta_schema": "meta/aws/guardduty", "meta_schema_version": "1.0.0"}},
                "2.0.0",
                {"1.0.0", "2.0.0"},
                [(("2.0.0", "schema_config", "meta_schema"), "meta/aws/guardduty")],
                id="a-draft-current-is-edited-in-place",
            ),
            pytest.param(
                {
                    "1.0.0": {
                        "schema": {},
                        "transform": {"engine": "vector", "config_file": "/a.toml"},
                    }
                },
                "1.0.0",
                {"transform": {"engine": "vector", "config_file": "/b.toml"}},
                "2.0.0",
                {"1.0.0", "2.0.0"},
                [(("2.0.0", "transform", "config_file"), "/b.toml")],
                id="transform-bumps-a-deployed-version",
            ),
        ],
    )
    def test_apply_write_update_version_cutting(self, stored, deployed, body, current, ids, checks):
        updated = apply_source_write_update(_stored(stored, deployed=deployed), _write(**body))

        assert updated.current == current
        assert updated.versions.keys() == ids
        assert updated.deployed_version == deployed
        assert updated.description == body.get("description")
        for path, expected in checks:
            assert _at(updated, *path) == expected

    def test_apply_write_update_omitted_header_keeps_the_stored_one(self):
        existing = _stored(
            {
                "1.0.0": {
                    "header": {"type": "timeseries", "version": "1.0.0"},
                    "schema": {"engine": "MergeTree"},
                }
            }
        )
        updated = apply_source_write_update(
            existing, _write(schema={"engine": "MergeTree", "ttl_days": 30})
        )
        ver = updated.versions["1.0.0"]
        assert ver.header == SourceHeader(type="timeseries", version="1.0.0")
        assert ver.to_yaml_dict()["header"] == {"type": "timeseries", "version": "1.0.0"}
        assert present(ver.schema_config).ttl_days == 30

    @pytest.mark.parametrize(
        ("before", "after", "bumps"),
        [
            ({}, {"schema": {"ttl_days": 30, "engine": "MergeTree"}}, True),
            ({}, {"schema": {"ttl_days": 90, "engine": "ReplacingMergeTree"}}, True),
            ({}, {"header": {"type": "minimal", "version": "1.0.0"}}, True),
            ({}, {"transform": {"engine": "vector"}}, True),
            ({}, {"views": [SIGMA_VIEW]}, True),
            (
                {"views": [SIGMA_VIEW]},
                {"views": [{**SIGMA_VIEW, "custom_mappings": {"User": "u"}}]},
                True,
            ),
            ({"views": [SIGMA_VIEW]}, {"views": [{**SIGMA_VIEW, "service": "sysmon"}]}, True),
            ({"views": [SIGMA_VIEW]}, {"views": [SIGMA_VIEW]}, False),
            ({}, {}, False),
            ({}, {"transport": "direct"}, False),
            ({}, {"archive": True}, False),
            (
                {"transform": ELASTIC},
                {"transform": {"engine": "elastic", "mode": "agentless"}},
                False,
            ),
            (
                {"transform": ELASTIC},
                {
                    "transform": {
                        "engine": "elastic",
                        "mode": "agentless",
                        "detect": {"unmatched": "stop"},
                    }
                },
                False,
            ),
            ({"transform": ELASTIC}, {"transform": {**ELASTIC, "mode": "agent"}}, False),
            (
                {"transform": {"engine": "vrl"}},
                {"transform": {"engine": "vrl", "variant": "okta"}},
                False,
            ),
            ({"transform": {"engine": "vrl"}}, {"transform": {"engine": "vector"}}, True),
            (
                {"fetcher": {"source_type": "okta"}},
                {
                    "fetcher": {
                        "source_type": "okta",
                        "routes": [{"match": MATCH | {"field": "eventType"}, "source": "other"}],
                    }
                },
                False,
            ),
        ],
        ids=[
            "ttl",
            "engine",
            "header",
            "transform-added",
            "views-added",
            "view-mappings",
            "view-service",
            "views-unchanged",
            "unchanged",
            "transport",
            "archive",
            "mode",
            "detect",
            "mode-agent",
            "variant",
            "transform-engine",
            "fetcher-route",
        ],
    )
    def test_source_version_bump_required(self, before, after, bumps):
        """A version id pins the shape of a table, so routing changes must not bump it."""
        base = {
            "header": {"type": "timeseries", "version": "1.0.0"},
            "schema": {"ttl_days": 90, "engine": "MergeTree"},
        }

        assert (
            source_version_bump_required(
                _version(**base, **before), _version(**{**base, **before, **after})
            )
            is bumps
        )

    def test_draft_build_version_to_invalidate(self):
        existing = _stored(
            {"1.0.0": {"schema": {}}, "2.0.0": {"schema": {"meta_schema": "meta/a"}}},
            deployed="1.0.0",
        )
        updated = existing.model_copy(deep=True)
        updated.versions["2.0.0"] = updated.versions["2.0.0"].model_copy(
            update={"schema_config": SourceSchema(meta_schema="meta/b")}
        )
        assert draft_build_version_to_invalidate(existing, updated) == "2.0.0"
        updated.versions["2.0.0"] = existing.versions["2.0.0"].model_copy(
            update={"match": SourceMatch(field="g", value="v")}
        )
        assert draft_build_version_to_invalidate(existing, updated) is None

    def test_draft_build_invalidate_before_first_deploy(self):
        schema = {"meta_schema": "meta/a", "meta_schema_version": "1.0.0"}
        existing = _stored({"1.0.0": {"schema": schema}})
        updated = existing.model_copy(deep=True)
        updated.versions["1.0.0"] = updated.versions["1.0.0"].model_copy(
            update={
                "schema_config": SourceSchema(meta_schema="meta/b", meta_schema_version="1.0.0")
            }
        )
        assert draft_build_version_to_invalidate(existing, updated) == "1.0.0"


class TestDeployedVersionOnWrite:
    """A source landing on the shared main table with no meta_schema owns no table to deploy."""

    @pytest.mark.parametrize(
        ("body", "deployed"),
        [
            ({"fetcher": OKTA_MAIN}, "1.0.0"),
            ({"fetcher": {"source_type": "okta", "topic": "own"}}, None),
            (
                {
                    "fetcher": OKTA_MAIN,
                    "schema": {"meta_schema": "meta/okta/system", "meta_schema_version": "1.0.0"},
                },
                None,
            ),
            ({"match": {"field": "_source", "value": "syslog"}}, None),
        ],
        ids=["main-fetcher", "own-topic-fetcher", "main-fetcher-with-meta-schema", "receiver"],
    )
    def test_a_source_is_deployed_from_creation_only_when_it_owns_no_table(self, body, deployed):
        src = source_from_write(_write(source="okta-main", **body), source_name="okta-main")

        assert src.current == "1.0.0"
        assert src.deployed_version == deployed

    @pytest.mark.parametrize(
        ("topic", "stored_deployed", "body", "current", "deployed"),
        [
            (
                "main",
                "1.0.0",
                {"fetcher": OKTA_MAIN, "views": [{"standard": "sigma", "taxonomy": "okta"}]},
                "2.0.0",
                "2.0.0",
            ),
            (
                "main",
                "1.0.0",
                {"fetcher": {"source_type": "okta", "topic": "own"}},
                "1.0.0",
                "1.0.0",
            ),
            (
                "main",
                None,
                {"fetcher": OKTA_MAIN, "description": "doc-only edit"},
                "1.0.0",
                "1.0.0",
            ),
            (
                "own",
                None,
                {
                    "fetcher": {"source_type": "okta", "topic": "own"},
                    "description": "doc-only edit",
                },
                "1.0.0",
                None,
            ),
        ],
        ids=[
            "bumped-main-tracks-the-new-current",
            "moving-to-own-keeps-the-deployed-version",
            "undeployed-main-picks-up-current",
            "undeployed-own-table-stays-undeployed",
        ],
    )
    def test_a_save_derives_the_deployed_version(
        self, topic, stored_deployed, body, current, deployed
    ):
        existing = _stored(
            {"1.0.0": {"fetcher": {"source_type": "okta", "topic": topic}, "schema": {}}},
            deployed=stored_deployed,
        )

        updated = apply_source_write_update(existing, _write(**body))

        assert updated.current == current
        assert updated.deployed_version == deployed


class TestLandingTopic:
    """A source landing on the shared topic arrives there but never owns it."""

    @pytest.mark.parametrize(
        ("body", "topic", "owns"),
        [
            (
                {"source": "aws-cloudtrail", "fetcher": {"source_type": "aws", "topic": "main"}},
                "main_land",
                False,
            ),
            (
                {"source": "okta-audit", "fetcher": {"source_type": "okta", "topic": "own"}},
                "okta-audit_land",
                True,
            ),
            ({"source": "syslog"}, "syslog_land", True),
            # The core `main` source is named for the landing table, so the shared
            # topic has an owner and the sources landing on it do not need one.
            ({"source": DEFAULT_LANDING_LABEL}, "main_land", True),
        ],
        ids=["main-fetcher", "own-fetcher", "receiver", "landing-source"],
    )
    def test_topic_and_ownership(self, body, topic, owns):
        s = _source(**body)

        assert s.topic_land == topic
        assert s.owns_landing_topic is owns


class TestFlowFields:
    def test_flow_fields_round_trip_through_a_write(self):
        snapshot = _write(
            source="auth",
            transport="direct",
            archive=False,
            transform={"engine": "vrl", "variant": "okta_system"},
        ).to_version_snapshot()

        assert snapshot.transport == "direct"
        assert snapshot.archive is False
        assert present(snapshot.transform).variant == "okta_system"

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            pytest.param(
                {"description": "doc-only edit"},
                {"transport": "direct", "archive": True, "description": "doc-only edit"},
                id="omitted-flow-fields-are-kept",
            ),
            pytest.param(
                {"transport": None, "archive": False},
                {"transport": None, "archive": False},
                id="sent-flow-fields-turn-them-off",
            ),
            pytest.param(
                {"transform": None}, {"transform": None}, id="null-transform-releases-the-app"
            ),
            pytest.param(
                {"description": "doc-only edit"},
                {"variant": "okta_system"},
                id="omitted-transform-is-kept",
            ),
        ],
    )
    def test_a_put_keeps_what_it_omits_and_clears_what_it_nulls(self, body, expected):
        existing = _source(
            source="auth",
            transport="direct",
            archive=True,
            transform={"engine": "vrl", "variant": "okta_system"},
        )

        updated = apply_source_write_update(existing, _write(**body))

        actual = {
            "transport": updated.transport,
            "archive": updated.archive,
            "description": updated.description,
            "transform": updated.transform,
            "variant": getattr(updated.transform, "variant", None),
        }
        assert {name: actual[name] for name in expected} == expected

    def test_a_flat_document_carries_the_flow_fields_into_its_version(self):
        source = _source(source="auth", transport="bus", archive=True)

        assert source.versions["1.0.0"].transport == "bus"
        assert source.archive is True

    def test_a_fetcher_route_validates_the_source_it_names(self):
        with pytest.raises(ValueError, match="DNS-1123 label"):
            FetcherRoute.model_validate({"match": MATCH, "source": "Not_A_Label"})


class TestOmittedFieldsKeepTheStoredValue:
    """A PUT keeps what its body leaves out. Only an explicit empty value clears it."""

    STORED: dict[str, Any] = {
        "header": {"type": "minimal", "version": "1.0.0"},
        "schema": {"ttl_days": 30},
        "views": [{"standard": "sigma", "taxonomy": "windows", "field_map": "sigma/windows"}],
        "transform": {"engine": "vrl"},
    }
    OWN_TABLE = {"meta_schema": "meta/aws/cloudtrail", "meta_schema_version": "1.0.0"}

    def _deployed(self, schema: dict) -> Source:
        stored = {**self.STORED, "schema": {**self.STORED["schema"], **schema}}
        return _stored({"1.0.0": stored}, deployed="1.0.0")

    @pytest.mark.parametrize(
        "schema",
        [pytest.param({}, id="shared-table"), pytest.param(OWN_TABLE, id="own-table")],
    )
    def test_an_unchanged_save_keeps_every_field_and_cuts_no_version(self, schema):
        existing = self._deployed(schema)

        updated = apply_source_write_update(existing, _write(description="doc-only edit"))

        assert updated.versions.keys() == {"1.0.0"}
        assert updated.current == "1.0.0"
        kept = updated.versions["1.0.0"].model_dump(exclude={"date_time"})
        assert kept == existing.versions["1.0.0"].model_dump(exclude={"date_time"})

    @pytest.mark.parametrize("cleared_views", [[], None], ids=["empty-list", "null"])
    def test_an_explicit_empty_value_clears_each_field(self, cleared_views):
        existing = self._deployed({})

        updated = apply_source_write_update(
            existing, _write(views=cleared_views, header=None, schema=None, transform=None)
        )

        assert updated.current == "2.0.0"
        cleared = updated.versions["2.0.0"]
        assert cleared.views == []
        assert cleared.header is None
        assert cleared.schema_config == SourceSchema()
        assert cleared.transform is None
        assert updated.versions["1.0.0"].views[0].field_map == "sigma/windows"

    def test_a_draft_keeps_its_own_fields_not_the_deployed_ones(self):
        draft = {
            **self.STORED,
            "header": {"type": "timeseries", "version": "1.0.0"},
            "transform": {"engine": "vrl", "variant": "okta_system"},
        }
        existing = _stored({"1.0.0": self.STORED, "2.0.0": draft}, deployed="1.0.0")

        updated = apply_source_write_update(existing, _write())

        kept = updated.versions["2.0.0"]
        assert kept.header == SourceHeader(type="timeseries", version="1.0.0")
        assert present(kept.transform).variant == "okta_system"


class TestSourceVersionGetResponse:
    def test_round_trip_fields(self):
        resp = SourceVersionGetResponse(
            source="my-source",
            display_name="My Source",
            description="desc",
            enabled=False,
            current="2.0.0",
            deployed_version="1.0.0",
            selected="1.0.0",
            versions=["1.0.0", "2.0.0"],
            current_table_topic_type="main",
            version=SourceVersion(
                date_time="2026-06-12",
                header=SourceHeader(type="timeseries", version="1.0.0"),
                schema_config=SourceSchema(engine="MergeTree"),
                match=SourceMatch(field="ingest_type", value="x"),
                transform=SourceTransform(engine="vector"),
            ),
        )
        assert resp.source == "my-source"
        assert resp.current_table_topic_type == "main"
        assert present(resp.version.schema_config).engine == "MergeTree"
        assert present(resp.version.match).value == "x"


class TestSourceSummaryPage:
    def test_from_summaries_pagination_and_tree(self):
        objs = [
            SourceSummaryObject(
                name=name,
                current="1.0.0",
                deployed_version="1.0.0",
                versions=["1.0.0"],
                current_table_topic_type=topic,
            )
            for name, topic in (("aws-cloudtrail", "own"), ("syslog", "main"))
        ]
        resp = SourceSummaryPage.from_summaries(objs, ListQuery(page_size=1), {})
        assert resp.total_size == 2
        assert len(resp.items) == 1
        root_names = {obj.name for obj in resp.objects.items}
        assert root_names == {"aws-cloudtrail", "syslog"}
        assert resp.objects.children == {}


class TestSourceVersioning:
    def test_versioned_input_migrates_top_level_match_and_transform(self):
        s = Source.model_validate(
            {
                "source": "legacy-top",
                "current": "1.0.0",
                "deployed_version": "1.0.0",
                "match": MATCH,
                "transform": {"engine": "vector"},
                "versions": {"1.0.0": {"date_time": "2026-01-01", "match": MATCH}},
            }
        )
        assert present(s.versions["1.0.0"].match).value == "v"
        assert s.versions["1.0.0"].transform is not None
        out = s.to_yaml_dict()
        assert "match" not in out
        assert out["versions"]["1.0.0"]["match"]["value"] == "v"

    def test_versioned_yaml_shape(self):
        s = Source.model_validate(
            {
                "source": "no-transform",
                "display_name": "No Transform",
                "enabled": True,
                "deployed_version": "1.0.0",
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-06-10",
                        "match": MATCH,
                        "transform": {"engine": "vector", "config_file": "/etc/vector/x.yaml"},
                        "schema": {
                            "meta_schema": "meta/aws/cloudwatch_logs",
                            "meta_schema_version": "1.0.0",
                            "engine": "MergeTree",
                        },
                    }
                },
            }
        )
        out = s.to_yaml_dict()
        ver = out["versions"]["1.0.0"]
        assert out["deployed_version"] == "1.0.0"
        assert ver["date_time"] == "2026-06-10"
        assert ver["schema"]["meta_schema"] == "meta/aws/cloudwatch_logs"
        assert ver["transform"]["engine"] == "vector"
        assert ver["transform"]["config_file"] == "/etc/vector/x.yaml"
        assert s.fetcher is None

    @pytest.mark.parametrize(
        "schema_key", ["schema", "schema_config"], ids=["schema", "schema_config-alias"]
    )
    def test_a_flat_document_normalizes_to_one_version(self, schema_key):
        s = Source.model_validate(
            {
                "source": "syslog",
                "match": MATCH,
                schema_key: {"ttl_days": 45, "engine": "MergeTree"},
            }
        )
        assert s.versions.keys() == {"1.0.0"}
        assert s.schema_config.ttl_days == 45
        assert s.current == "1.0.0"
        assert s.deployed_version is None

    def test_a_flat_document_moves_versioned_keys_into_the_snapshot(self):
        """Guards _VERSIONED_KEYS: flat bodies normalize into the version tree."""
        s = Source.model_validate(
            {
                "source": "pull-src",
                "fetcher": {"source_type": "m365", "config": {"services": [{"name": "alerts"}]}},
                "views": [{"standard": "sigma", "taxonomy": "windows"}],
            }
        )
        ver = s.versions["1.0.0"]
        assert present(ver.fetcher).config == {"services": [{"name": "alerts"}]}
        assert ver.origin == "fetcher"
        assert present(ver.view_for("sigma")).taxonomy == "windows"

    @pytest.mark.parametrize(
        ("pointers", "versions", "current", "deployed"),
        [
            ({"deployed_version": "1.0.0"}, ["1.0.0"], "1.0.0", "1.0.0"),
            ({"current": "2.0.0"}, ["2.0.0"], "2.0.0", None),
        ],
        ids=["current-defaults-from-deployed", "deployed-stays-unset"],
    )
    def test_versioned_input_pointers(self, pointers, versions, current, deployed):
        s = Source.model_validate(
            {
                "source": "x",
                **pointers,
                "versions": {vid: {"date_time": "2026-01-01", "match": MATCH} for vid in versions},
            }
        )
        assert s.current == current
        assert s.deployed_version == deployed

    @pytest.mark.parametrize(
        ("pointers", "refusal"),
        [
            ({"current": "2.0.0", "deployed_version": "1.0.0"}, r"current version '2\.0\.0'"),
            ({"current": "1.0.0", "deployed_version": "9.9.9"}, r"deployed_version '9\.9\.9'"),
        ],
        ids=["current-missing", "deployed-missing"],
    )
    def test_version_pointers_must_name_a_defined_version(self, pointers, refusal):
        with pytest.raises(ValueError, match=refusal):
            Source.model_validate(
                {
                    "source": "x",
                    **pointers,
                    "versions": {"1.0.0": {"date_time": "2026-01-01", "match": MATCH}},
                }
            )

    def test_before_validator_passes_a_non_dict_to_pydantic(self):
        with pytest.raises(ValidationError):
            Source.model_validate(42)

    def test_empty_versions_rejected(self):
        src = Source.model_construct(
            source="empty-ver",
            display_name="Empty",
            enabled=True,
            current="1.0.0",
            deployed_version="1.0.0",
            versions={},
        )
        with pytest.raises(ValueError, match="at least one"):
            Source._validate_versions_and_display_name(src)  # ty: ignore[call-non-callable]  # pydantic leaves the validator callable on the built class

    def test_version_lookup_unknown_id(self):
        with pytest.raises(ValueError, match=r"Source version '9\.9\.9' is not defined"):
            _source().version("9.9.9")
