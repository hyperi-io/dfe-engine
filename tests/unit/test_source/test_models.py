"""Tests for Source Pydantic models."""

from typing import get_args

import pytest
from pydantic import ValidationError

from dfe_engine.appmgmt.catalogue import transform_engines
from dfe_engine.source.models import (
    DEFAULT_LANDING_LABEL,
    FetcherRoute,
    FetcherTopic,
    PaginatedSourceSummaryResponse,
    SchemaColumn,
    Source,
    SourceFetcher,
    SourceHeader,
    SourceMatch,
    SourceSchema,
    SourceSummaryObject,
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


@pytest.fixture
def registry() -> TypeRegistry:
    return TypeRegistry.default()


# ---------------------------------------------------------------------------
# SchemaColumn
# ---------------------------------------------------------------------------


class TestSchemaColumn:
    def test_minimal(self):
        col = SchemaColumn(name="user_name", type="string")
        assert col.name == "user_name"
        assert col.type == "string"
        assert col.attribute == []
        assert col.use_case is None
        assert col.ch_override is None

    def test_full(self):
        col = SchemaColumn(
            name="source_ip",
            type="ip",
            attribute=["lowcardinality"],
            use_case="range",
            default="'::'",
            order=3,
            expr="@source: src_ip",
            comment="Source IP address",
            ch_override=None,
        )
        assert col.name == "source_ip"
        assert col.attribute == ["lowcardinality"]
        assert col.use_case == "range"
        assert col.order == 3
        assert col.expr == "@source: src_ip"
        assert col.comment == "Source IP address"

    def test_attribute_coercion_from_string(self):
        col = SchemaColumn(name="x", type="string", attribute="lowcardinality")
        assert col.attribute == ["lowcardinality"]

    def test_attribute_coercion_from_list(self):
        col = SchemaColumn(name="x", type="string", attribute=["lowcardinality", "nullable"])
        assert col.attribute == ["lowcardinality", "nullable"]

    def test_validate_valid_ch_override(self, registry: TypeRegistry):
        col = SchemaColumn(name="x", type="string", ch_override="String")
        errors = col.validate_against_registry(registry)
        assert errors == []

    def test_attribute_coercion_from_none(self):
        col = SchemaColumn(name="x", type="string", attribute=None)
        assert col.attribute == []

    def test_validate_valid_column(self, registry: TypeRegistry):
        col = SchemaColumn(name="x", type="string", use_case="dimension")
        errors = col.validate_against_registry(registry)
        assert errors == []
        col = SchemaColumn(name="x", type="integer", use_case="fulltext")
        errors = col.validate_against_registry(registry)
        assert len(errors) == 1
        assert "fulltext" in errors[0]

    def test_validate_invalid_attribute(self, registry: TypeRegistry):
        col = SchemaColumn(name="x", type="json", attribute=["lowcardinality"])
        errors = col.validate_against_registry(registry)
        assert len(errors) == 1
        assert "lowcardinality" in errors[0]

    def test_validate_invalid_ch_override(self, registry: TypeRegistry):
        col = SchemaColumn(name="x", type="string", ch_override="NotAType")
        errors = col.validate_against_registry(registry)
        assert len(errors) == 1
        assert "NotAType" in errors[0]

    def test_validate_unknown_primitive(self, registry: TypeRegistry):
        col = SchemaColumn(name="x", type="nonexistent")
        errors = col.validate_against_registry(registry)
        assert len(errors) == 1
        assert "unknown primitive" in errors[0]

    def test_validate_multiple_errors(self, registry: TypeRegistry):
        col = SchemaColumn(
            name="x",
            type="json",
            attribute=["lowcardinality"],
            use_case="dimension",
        )
        errors = col.validate_against_registry(registry)
        assert len(errors) == 2


# ---------------------------------------------------------------------------
# SourceHeader
# ---------------------------------------------------------------------------


class TestSourceHeader:
    def test_defaults(self):
        h = SourceHeader()
        assert h.type == "timeseries"
        assert h.version == "1.0.0"

    def test_custom(self):
        h = SourceHeader(type="minimal", version="2.0.0")
        assert h.type == "minimal"
        assert h.version == "2.0.0"


# ---------------------------------------------------------------------------
# SourceMatch
# ---------------------------------------------------------------------------


class TestSourceMatch:
    def test_creation(self):
        m = SourceMatch(field="tags.collector.type", value="filebeat")
        assert m.field == "tags.collector.type"
        assert m.value == "filebeat"
        assert m.operator == "equals"

    def test_exists_operator_allows_empty_value(self):
        m = SourceMatch(field="_json.tags.type", operator="exists", value="")
        assert m.operator == "exists"
        assert m.value == ""

    def test_value_required_for_equals(self):
        with pytest.raises(ValueError, match=r"match\.value is required"):
            SourceMatch(field="_json.f", operator="includes", value="  ")


# ---------------------------------------------------------------------------
# SourceSchema
# ---------------------------------------------------------------------------


class TestSourceSchema:
    def test_minimal(self):
        s = SourceSchema(ttl_days=90)
        assert s.ttl_days == 90
        assert s.engine == ""  # empty = inherit the deployment default at DDL time
        assert s.meta_schema is None

    def test_zero_ttl_is_accepted_and_negative_refused(self):
        assert SourceSchema(ttl_days=0).ttl_days == 0
        with pytest.raises(ValidationError):
            SourceSchema(ttl_days=-1)

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

    def test_parameterised_engine(self):
        # A parameterised variant is accepted - the registry gates the variant,
        # the params (a version column here) are the caller's.
        s = SourceSchema(engine="ReplacingMergeTree(_timestamp_load)")
        assert s.engine == "ReplacingMergeTree(_timestamp_load)"

    def test_invalid_engine(self):
        with pytest.raises(ValueError, match="Invalid engine"):
            SourceSchema(engine="InvalidEngine")

    def test_replicated_prefix_rejected(self):
        # The Replicated/Shared prefix is topology-derived at DDL time, never
        # declared in source config - so it is not a permitted variant.
        with pytest.raises(ValueError, match="Invalid engine"):
            SourceSchema(engine="ReplicatedMergeTree")


# ---------------------------------------------------------------------------
# SourceTransform
# ---------------------------------------------------------------------------


class TestSourceTransform:
    def test_vector(self):
        t = SourceTransform(engine="vector", config_file="/etc/vector/fb.yaml")
        assert t.engine == "vector"
        assert t.env == {}
        assert t.files == []

    def test_vrl(self):
        t = SourceTransform(engine="vrl")
        assert t.engine == "vrl"

    def test_every_catalogued_transform_is_accepted(self):
        engines = transform_engines()
        assert engines
        for engine in engines:
            assert SourceTransform(engine=engine).engine == engine

    def test_invalid_engine_names_the_catalogue(self):
        with pytest.raises(ValueError, match="Invalid transform engine") as exc:
            SourceTransform(engine="spark")
        assert ", ".join(sorted(transform_engines())) in str(exc.value)


# ---------------------------------------------------------------------------
# SourceFetcher
# ---------------------------------------------------------------------------


class TestSourceFetcher:
    def test_minimal(self):
        f = SourceFetcher(source_type="crowdstrike")
        assert f.source_type == "crowdstrike"
        assert f.topic == "own"
        assert f.config == {}
        assert f.landing_label("edr") == "edr"

    def test_main_topic_lands_on_the_shared_landing(self):
        f = SourceFetcher(source_type="okta", topic="main")
        assert f.landing_label("okta-audit") == "main"

    def test_the_topic_literal_still_carries_the_landing_label(self):
        # FetcherTopic cannot be built from a constant, so it restates the label.
        assert DEFAULT_LANDING_LABEL in get_args(FetcherTopic)

    def test_unknown_source_type_is_refused(self):
        # The manifest lists the families the fetcher ships; anything else has
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


class TestSourceOrigin:
    def test_an_operator_source_needs_exactly_one_origin(self):
        with pytest.raises(ValueError, match="exactly one"):
            Source.model_validate({"source": "x"})
        with pytest.raises(ValueError, match="not both"):
            Source.model_validate(
                {
                    "source": "x",
                    "match": {"field": "f", "value": "v"},
                    "fetcher": {"source_type": "okta"},
                }
            )

    def test_a_core_source_may_have_no_origin_at_all(self):
        s = Source.model_validate({"source": "main", "resource_type": "core"})

        assert s.origin is None
        assert s.match is None
        assert s.fetcher is None
        assert s.model_dump(mode="json")["origin"] is None

    def test_a_core_source_still_cannot_declare_both(self):
        with pytest.raises(ValueError, match="not both"):
            Source.model_validate(
                {
                    "source": "main",
                    "resource_type": "core",
                    "match": {"field": "f", "value": "v"},
                    "fetcher": {"source_type": "okta"},
                }
            )

    def test_a_match_rule_makes_a_receiver_source(self):
        s = Source(source="syslog", match=SourceMatch(field="f", value="v"))
        assert s.origin == "receiver"
        assert s.fetcher is None
        assert s.landing_label() == "syslog"
        assert s.model_dump(mode="json")["origin"] == "receiver"

    def test_a_fetcher_makes_a_fetcher_source(self):
        s = Source.model_validate(
            {"source": "okta-audit", "fetcher": {"source_type": "okta", "topic": "main"}}
        )
        assert s.origin == "fetcher"
        assert s.match is None
        assert s.landing_label() == "main"
        assert "match" not in s.to_yaml_dict()["versions"]["1.0.0"]

    def test_the_write_body_needs_exactly_one_origin(self):
        with pytest.raises(ValueError, match="exactly one"):
            SourceWriteRequest(source="x")
        body = SourceWriteRequest(source="x", fetcher=SourceFetcher(source_type="pypi"))
        assert body.to_version_snapshot().origin == "fetcher"


# ---------------------------------------------------------------------------
# SourceView
# ---------------------------------------------------------------------------


class TestSourceView:
    def test_defaults(self):
        v = SourceView(standard="sigma")
        assert v.standard == "sigma"
        assert v.field_map is None
        assert v.custom_mappings == {}
        assert v.taxonomy is None
        assert v.category is None
        assert v.service is None

    def test_with_mappings(self):
        v = SourceView(
            standard="sigma",
            taxonomy="windows",
            custom_mappings={"CommandLine": "command_line"},
        )
        assert v.taxonomy == "windows"
        assert v.custom_mappings["CommandLine"] == "command_line"

    def test_standard_forced_lowercase(self):
        v = SourceView(standard="ECS")
        assert v.standard == "ecs"

    def test_unknown_standard_rejected(self):
        with pytest.raises(ValueError, match="Unknown view standard"):
            SourceView(standard="splunk_cim_v9")

    def test_field_map_path(self):
        v = SourceView(standard="ecs", field_map="ecs/filebeat")
        assert v.field_map == "ecs/filebeat"


# ---------------------------------------------------------------------------
# Source — Top-Level
# ---------------------------------------------------------------------------


class TestSource:
    def test_minimal(self):
        s = Source(
            source="syslog",
            match=SourceMatch(field="tags.collector.type", value="syslog"),
        )
        assert s.source == "syslog"
        assert s.enabled is True
        assert s.display_name == "Syslog"
        assert s.version().header is None
        assert s.header == SourceHeader()
        assert s.schema_config.engine == ""  # empty = inherit deployment default

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
                "transform": {
                    "engine": "vector",
                    "config_file": "/etc/vector/filebeat.yaml",
                },
                "views": [
                    {
                        "standard": "sigma",
                        "taxonomy": "linux",
                        "custom_mappings": {"User": "user_name"},
                    }
                ],
            }
        )
        assert s.source == "filebeat"
        assert s.display_name == "Elastic Filebeat"
        assert s.schema_config.meta_schema == "logs_beats_filebeat"
        assert s.transform.engine == "vector"
        assert s.view_for("sigma") is not None
        assert s.view_for("sigma").taxonomy == "linux"

    def test_derived_properties(self):
        s = Source(
            source="filebeat",
            match=SourceMatch(field="f", value="v"),
            transform=SourceTransform(engine="vector"),
        )
        assert s.topic_land == "filebeat_land"
        assert s.topic_load == "filebeat_load"
        assert s.table_name == "filebeat"

    def test_no_transform_no_load_topic(self):
        s = Source(source="syslog", match=SourceMatch(field="f", value="v"))
        assert s.topic_land == "syslog_land"
        assert s.topic_load is None

    def test_display_name_auto(self):
        s = Source(source="crowdstrike-edr", match=SourceMatch(field="f", value="v"))
        assert s.display_name == "Crowdstrike Edr"

    def test_display_name_explicit(self):
        s = Source(
            source="crowdstrike-edr",
            display_name="CrowdStrike EDR",
            match=SourceMatch(field="f", value="v"),
        )
        assert s.display_name == "CrowdStrike EDR"

    def test_views_default_empty(self):
        s = Source(source="syslog", match=SourceMatch(field="f", value="v"))
        assert s.views == []
        assert s.view_for("sigma") is None

    def test_state_default_active(self):
        s = Source(source="syslog", match=SourceMatch(field="f", value="v"))
        assert s.state == "active"
        assert s.enabled is True

    def test_legacy_enabled_false_maps_to_disabled(self):
        s = Source(source="syslog", enabled=False, match=SourceMatch(field="f", value="v"))
        assert s.state == "disabled"
        assert s.enabled is False

    def test_state_wins_over_enabled(self):
        s = Source.model_validate(
            {
                "source": "syslog",
                "enabled": True,
                "state": "dormant",
                "match": {"field": "f", "value": "v"},
            }
        )
        assert s.state == "dormant"
        assert s.enabled is False  # compat accessor: enabled == (state == active)

    def test_state_in_yaml_dict(self):
        s = Source.model_validate(
            {"source": "syslog", "state": "dormant", "match": {"field": "f", "value": "v"}}
        )
        d = s.to_yaml_dict()
        assert d["state"] == "dormant"
        assert "enabled" not in d
        # round-trips
        assert Source.model_validate(d).state == "dormant"

    def test_enabled_serialized_on_api_dump(self):
        s = Source.model_validate(
            {"source": "syslog", "state": "dormant", "match": {"field": "f", "value": "v"}}
        )
        dumped = s.model_dump(mode="json")
        assert dumped["state"] == "dormant"
        assert dumped["enabled"] is False

    def test_views_set(self):
        s = Source(
            source="syslog",
            match=SourceMatch(field="f", value="v"),
            views=[SourceView(standard="sigma"), SourceView(standard="ecs")],
        )
        assert [v.standard for v in s.views] == ["sigma", "ecs"]

    def test_views_in_yaml_dict(self):
        s = Source(
            source="syslog",
            match=SourceMatch(field="f", value="v"),
            views=[SourceView(standard="sigma")],
        )
        d = s.to_yaml_dict()
        assert d["versions"]["1.0.0"]["views"] == [{"standard": "sigma"}]

    def test_views_excluded_when_empty(self):
        s = Source(source="syslog", match=SourceMatch(field="f", value="v"))
        d = s.to_yaml_dict()
        assert "views" not in d["versions"]["1.0.0"]


# ---------------------------------------------------------------------------
# Source — Naming Validation
# ---------------------------------------------------------------------------


class TestSourceNaming:
    """A source name has to survive as the DNS-1123 label an instance is named for."""

    def test_valid_names(self):
        for name in ["filebeat", "syslog", "crowdstrike-edr", "a", "x123-456"]:
            s = Source(source=name, match=SourceMatch(field="f", value="v"))
            assert s.source == name

    def test_starts_with_digit(self):
        with pytest.raises(ValueError, match="DNS-1123 label"):
            Source(source="123abc")

    def test_uppercase(self):
        with pytest.raises(ValueError, match="DNS-1123 label"):
            Source(source="FileBeat")

    def test_underscores_are_refused_and_the_error_says_why(self):
        """The message has to name the constraint, not just quote the regex."""
        with pytest.raises(ValueError) as caught:
            Source(source="crowdstrike_edr", match=SourceMatch(field="f", value="v"))
        message = str(caught.value)
        assert "DNS-1123 label" in message
        assert "use '-' instead of '_'" in message
        # It says what breaks, so the reader knows why the rule exists.
        assert "instance" in message

    def test_trailing_hyphen(self):
        """A DNS-1123 label ends alphanumeric, so the instance would be rejected."""
        with pytest.raises(ValueError, match="DNS-1123 label"):
            Source(source="filebeat-")

    def test_spaces(self):
        with pytest.raises(ValueError, match="DNS-1123 label"):
            Source(source="file beat")

    def test_too_long(self):
        with pytest.raises(ValueError, match="exceeds max length"):
            Source(source="a" * 41)

    def test_max_length_ok(self):
        s = Source(source="a" * 40, match=SourceMatch(field="f", value="v"))
        assert len(s.source) == 40

    def test_empty(self):
        with pytest.raises(ValueError):
            Source(source="")

    def test_leading_underscore(self):
        with pytest.raises(ValueError, match="DNS-1123 label"):
            Source(source="_test")

    def test_every_legal_name_is_a_legal_instance_name(self):
        """The alignment this rule exists for, asserted against the instance rule."""
        from dfe_engine.appmgmt.instances import validate_instance

        for name in ["filebeat", "crowdstrike-edr", "x123-456", "a", "a" * 40]:
            Source(source=name, match=SourceMatch(field="f", value="v"))
            validate_instance(name)


# ---------------------------------------------------------------------------
# Source — YAML Serialisation
# ---------------------------------------------------------------------------


class TestSourceYaml:
    def test_round_trip(self):
        data = {
            "source": "filebeat",
            "match": {"field": "tags.collector.type", "value": "filebeat"},
            "header": {"type": "timeseries", "version": "1.0.0"},
            "schema": {"ttl_days": 90},
        }
        s = Source.model_validate(data)
        yaml_dict = s.to_yaml_dict()

        assert yaml_dict["source"] == "filebeat"
        assert yaml_dict["versions"]["1.0.0"]["match"]["field"] == "tags.collector.type"
        assert yaml_dict["versions"]["1.0.0"]["schema"]["ttl_days"] == 90
        assert "schema_config" not in yaml_dict
        assert "deployed_version" not in yaml_dict
        assert "current" in yaml_dict

    def test_excludes_none(self):
        s = Source(source="syslog", match=SourceMatch(field="f", value="v"))
        yaml_dict = s.to_yaml_dict()
        assert "match" not in yaml_dict
        assert "transform" not in yaml_dict
        assert "deployed_version" not in yaml_dict
        assert "fetcher" not in yaml_dict
        assert "views" not in yaml_dict
        assert "description" not in yaml_dict

    def test_round_trip_preserves_data(self):
        data = {
            "source": "test-source",
            "display_name": "Test Source",
            "description": "A test",
            "enabled": False,
            "match": {"field": "f", "value": "v"},
            "header": {"type": "minimal", "version": "2.0.0"},
            "schema": {
                "meta_schema": "test_meta",
                "ttl_days": 30,
                "engine": "SummingMergeTree",
            },
        }
        s1 = Source.model_validate(data)
        yaml_dict = s1.to_yaml_dict()
        s2 = Source.model_validate(yaml_dict)

        assert s1.source == s2.source
        assert s1.display_name == s2.display_name
        assert s1.enabled == s2.enabled
        assert s1.schema_config.engine == s2.schema_config.engine


class TestMaterialisationAction:
    def test_locked_rule(self):
        from dfe_engine.source.models import materialisation_action

        assert materialisation_action("active") == "create"
        assert materialisation_action("dormant") == "leave"
        assert materialisation_action("disabled") == "reclaim"


class TestSourceVersion:
    def test_to_yaml_dict_omits_empty_containers(self):
        ver = SourceVersion(
            date_time="2026-06-10",
            match=SourceMatch(field="f", value="v"),
            views=[],
        )
        out = ver.to_yaml_dict()
        assert "views" not in out
        assert out["date_time"] == "2026-06-10"

    def test_to_yaml_dict_prunes_empty_view_containers(self):
        ver = SourceVersion(
            date_time="2026-06-10",
            match=SourceMatch(field="f", value="v"),
            views=[SourceView(standard="sigma")],
        )
        out = ver.to_yaml_dict()
        assert out["views"] == [{"standard": "sigma"}]

    def test_view_for(self):
        ver = SourceVersion(
            date_time="2026-06-10",
            match=SourceMatch(field="f", value="v"),
            views=[SourceView(standard="sigma", taxonomy="windows")],
        )
        assert ver.view_for("sigma").taxonomy == "windows"
        assert ver.view_for("ecs") is None

    def test_duplicate_view_standards_rejected(self):
        """Duplicate standards would let view_for (first wins) and the DDL
        overlay (last wins) silently disagree - refused outright."""
        with pytest.raises(ValueError, match="duplicate view"):
            SourceVersion(
                date_time="2026-01-01",
                match=SourceMatch(field="f", value="v"),
                views=[
                    SourceView(standard="sigma", taxonomy="windows"),
                    SourceView(standard="sigma", taxonomy="linux"),
                ],
            )


class TestSourceWriteRequest:
    def test_rejects_version_tree_keys(self):
        with pytest.raises(ValueError, match="not allowed"):
            SourceWriteRequest.model_validate(
                {
                    "source": "x",
                    "versions": {"1.0.0": {}},
                }
            )

    def test_rejects_deployed_version_on_write(self):
        with pytest.raises(ValueError, match="not allowed"):
            SourceWriteRequest.model_validate(
                {"source": "x", "deployed_version": "1.0.0"},
            )

    @pytest.mark.parametrize("removed_key", ["sigma", "mapping_standards", "field_mappings"])
    def test_rejects_removed_2_1_keys(self, removed_key):
        """Pre-2.2 mapping keys fail LOUDLY - extra=ignore would otherwise 200
        while the client's mapping config silently vanished."""
        with pytest.raises(ValueError, match=r"removed in 2\.2"):
            SourceWriteRequest.model_validate(
                {
                    "source": "x",
                    "match": {"field": "f", "value": "v"},
                    removed_key: {"taxonomy": "windows"},
                }
            )

    def test_apply_write_update_without_state_or_enabled_keeps_dormant(self):
        """A PUT that sends NEITHER state nor enabled must not re-activate a
        dormant source (effective_state keeps the current tri-state)."""
        existing = Source.model_validate(
            {
                "source": "dormant-src",
                "state": "dormant",
                "match": {"field": "f", "value": "v"},
            }
        )
        write = SourceWriteRequest.model_validate(
            {"description": "doc-only edit", "match": {"field": "f", "value": "v"}}
        )
        updated = apply_source_write_update(existing, write)
        assert updated.state == "dormant"
        assert updated.description == "doc-only edit"

    def test_create_uses_1_0_0_not_header_profile_version(self):
        write = SourceWriteRequest.model_validate(
            {
                "source": "profile-pin",
                "header": {"type": "common-header/minimal", "version": "1.1.0"},
                "match": {"field": "f", "value": "v"},
                "schema": {"engine": "MergeTree"},
            }
        )
        src = source_from_write(write, source_name="profile-pin")
        assert "1.0.0" in src.versions
        assert src.versions["1.0.0"].header.version == "1.1.0"
        assert src.current == "1.0.0"
        assert src.deployed_version is None

    def test_create_without_header_leaves_header_unset(self):
        write = SourceWriteRequest.model_validate(
            {
                "source": "no-header",
                "match": {"field": "f", "value": "v"},
                "schema": {"engine": "MergeTree"},
            }
        )
        src = source_from_write(write, source_name="no-header")
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
        existing = Source.model_validate(
            {
                "source": "src-a",
                "deployed_version": "1.0.0",
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "header": {"type": "timeseries", "version": "1.0.0"},
                        "match": {"field": "f", "value": "v"},
                        "schema": {},
                    },
                    "2.0.0": {
                        "date_time": "2026-01-02",
                        "header": {"type": "timeseries", "version": "1.0.0"},
                        "match": {"field": "f", "value": "v"},
                        "schema": {},
                    },
                },
            }
        )
        write = SourceWriteRequest.model_validate(
            {
                "match": {"field": "f", "value": "v"},
                "schema": {
                    "engine": "MergeTree",
                    "meta_schema_version": "2.0.0",
                },
            }
        )
        monkeypatch.setattr(
            "dfe_engine.source.models.next_major_source_version",
            lambda _versions: "2.0.0",
        )
        with pytest.raises(ValueError, match="Refusing to overwrite"):
            apply_source_write_update(existing, write)

    def test_apply_write_update_appends_on_bump_worthy_change_after_deploy(self):
        existing = Source.model_validate(
            {
                "source": "src-a",
                "deployed_version": "1.0.0",
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "header": {"type": "timeseries", "version": "1.0.0"},
                        "match": {"field": "f", "value": "v"},
                        "schema": {"ttl_days": 90, "meta_schema_version": "1.0.0"},
                    }
                },
            }
        )
        write = SourceWriteRequest.model_validate(
            {
                "enabled": True,
                "description": "rev 2",
                "match": {"field": "f", "value": "v"},
                "schema": {
                    "ttl_days": 30,
                    "engine": "MergeTree",
                    "meta_schema_version": "2.0.0",
                },
            }
        )
        updated = apply_source_write_update(existing, write)
        assert "1.0.0" in updated.versions
        assert updated.versions["1.0.0"].schema_config.ttl_days == 90
        assert "2.0.0" in updated.versions
        assert updated.versions["2.0.0"].schema_config.ttl_days == 30
        assert updated.versions["2.0.0"].schema_config.meta_schema_version == "2.0.0"
        assert updated.current == "2.0.0"
        assert updated.deployed_version == "1.0.0"
        assert updated.description == "rev 2"

    def test_apply_write_update_in_place_before_deploy(self):
        existing = Source.model_validate(
            {
                "source": "src-a",
                "deployed_version": None,
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "match": {"field": "f", "value": "v"},
                        "schema": {"meta_schema_version": "1.0.0"},
                    }
                },
            }
        )
        write = SourceWriteRequest.model_validate(
            {
                "match": {"field": "f", "value": "v"},
                "schema": {"meta_schema_version": "2.0.0"},
            }
        )
        updated = apply_source_write_update(existing, write)
        assert updated.versions.keys() == {"1.0.0"}
        assert updated.current == "1.0.0"
        assert updated.versions["1.0.0"].schema_config.meta_schema_version == "2.0.0"

    def test_apply_write_update_omitted_header_not_persisted(self):
        existing = Source.model_validate(
            {
                "source": "src-a",
                "deployed_version": None,
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "header": {"type": "timeseries", "version": "1.0.0"},
                        "match": {"field": "f", "value": "v"},
                        "schema": {"engine": "MergeTree"},
                    }
                },
            }
        )
        write = SourceWriteRequest.model_validate(
            {
                "match": {"field": "f", "value": "v"},
                "schema": {"engine": "MergeTree", "ttl_days": 30},
            }
        )
        updated = apply_source_write_update(existing, write)
        ver = updated.versions["1.0.0"]
        assert ver.header is None
        assert "header" not in ver.to_yaml_dict()
        assert ver.schema_config.ttl_days == 30

    def test_apply_write_update_in_place_after_deploy_non_bump_fields(self):
        existing = Source.model_validate(
            {
                "source": "src-a",
                "deployed_version": "1.0.0",
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "match": {"field": "f", "value": "v"},
                        "schema": {"ttl_days": 90, "meta_schema_version": "1.0.0"},
                    }
                },
            }
        )
        write = SourceWriteRequest.model_validate(
            {
                "match": {"field": "g", "value": "w"},
                "schema": {"ttl_days": 30, "meta_schema_version": "1.0.0"},
            }
        )
        updated = apply_source_write_update(existing, write)
        assert updated.versions.keys() == {"1.0.0"}
        assert updated.versions["1.0.0"].schema_config.ttl_days == 30
        assert updated.versions["1.0.0"].match.field == "g"

    def test_apply_write_update_bumps_on_meta_schema_path_after_deploy(self):
        existing = Source.model_validate(
            {
                "source": "src-a",
                "deployed_version": "2.0.0",
                "current": "2.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "match": {"field": "f", "value": "v"},
                        "schema": {},
                    },
                    "2.0.0": {
                        "date_time": "2026-01-02",
                        "match": {"field": "f", "value": "v"},
                        "schema": {
                            "meta_schema": "meta/aws/cloudwatch",
                            "meta_schema_version": "1.0.0",
                        },
                    },
                },
            }
        )
        write = SourceWriteRequest.model_validate(
            {
                "match": {"field": "f", "value": "v"},
                "schema": {
                    "meta_schema": "meta/aws/guardduty",
                    "meta_schema_version": "1.0.0",
                },
            }
        )
        updated = apply_source_write_update(existing, write)
        assert updated.current == "3.0.0"
        assert updated.versions["2.0.0"].schema_config.meta_schema == "meta/aws/cloudwatch"
        assert updated.versions["3.0.0"].schema_config.meta_schema == "meta/aws/guardduty"
        assert updated.deployed_version == "2.0.0"

    def test_apply_write_update_draft_current_no_bump_after_deploy(self):
        """Bump-worthy edits on a non-deployed ``current`` stay in place."""
        existing = Source.model_validate(
            {
                "source": "src-a",
                "deployed_version": "1.0.0",
                "current": "2.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "match": {"field": "f", "value": "v"},
                        "schema": {
                            "meta_schema": "meta/aws/cloudwatch",
                            "meta_schema_version": "1.0.0",
                        },
                    },
                    "2.0.0": {
                        "date_time": "2026-01-02",
                        "match": {"field": "f", "value": "v"},
                        "schema": {
                            "meta_schema": "meta/aws/cloudwatch",
                            "meta_schema_version": "1.0.0",
                        },
                    },
                },
            }
        )
        write = SourceWriteRequest.model_validate(
            {
                "match": {"field": "f", "value": "v"},
                "schema": {
                    "meta_schema": "meta/aws/guardduty",
                    "meta_schema_version": "1.0.0",
                },
            }
        )
        updated = apply_source_write_update(existing, write)
        assert updated.current == "2.0.0"
        assert "3.0.0" not in updated.versions
        assert updated.versions["2.0.0"].schema_config.meta_schema == "meta/aws/guardduty"
        assert updated.deployed_version == "1.0.0"

    def test_source_version_bump_required_transform(self):
        base = {
            "date_time": "2026-01-01",
            "match": {"field": "f", "value": "v"},
            "schema": {},
            "transform": {"engine": "vector", "config_file": "/a.toml"},
        }
        prev = SourceVersion.model_validate(base)
        updated = SourceVersion.model_validate(
            {**base, "transform": {"engine": "vector", "config_file": "/b.toml"}}
        )
        assert source_version_bump_required(prev, updated) is True

    def test_apply_write_update_bumps_transform_when_current_deployed(self):
        existing = Source.model_validate(
            {
                "source": "src-a",
                "deployed_version": "1.0.0",
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "match": {"field": "f", "value": "v"},
                        "schema": {},
                        "transform": {"engine": "vector", "config_file": "/a.toml"},
                    }
                },
            }
        )
        write = SourceWriteRequest.model_validate(
            {
                "match": {"field": "f", "value": "v"},
                "transform": {"engine": "vector", "config_file": "/b.toml"},
            }
        )
        updated = apply_source_write_update(existing, write)
        assert updated.current == "2.0.0"
        assert updated.versions["2.0.0"].transform is not None
        assert updated.versions["2.0.0"].transform.config_file == "/b.toml"
        assert updated.deployed_version == "1.0.0"

    def test_draft_build_version_to_invalidate(self):
        existing = Source.model_validate(
            {
                "source": "x",
                "deployed_version": "1.0.0",
                "current": "2.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "match": {"field": "f", "value": "v"},
                        "schema": {},
                    },
                    "2.0.0": {
                        "date_time": "2026-01-02",
                        "match": {"field": "f", "value": "v"},
                        "schema": {"meta_schema": "meta/a"},
                    },
                },
            }
        )
        updated = existing.model_copy(deep=True)
        updated.versions["2.0.0"] = updated.versions["2.0.0"].model_copy(
            update={
                "schema_config": SourceSchema(meta_schema="meta/b"),
            }
        )
        assert draft_build_version_to_invalidate(existing, updated) == "2.0.0"
        updated.versions["2.0.0"] = existing.versions["2.0.0"].model_copy(
            update={"match": SourceMatch(field="g", value="v")}
        )
        assert draft_build_version_to_invalidate(existing, updated) is None

    def test_draft_build_invalidate_before_first_deploy(self):
        existing = Source.model_validate(
            {
                "source": "x",
                "deployed_version": None,
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "match": {"field": "f", "value": "v"},
                        "schema": {"meta_schema": "meta/a", "meta_schema_version": "1.0.0"},
                    }
                },
            }
        )
        updated = existing.model_copy(deep=True)
        updated.versions["1.0.0"] = updated.versions["1.0.0"].model_copy(
            update={
                "schema_config": SourceSchema(meta_schema="meta/b", meta_schema_version="1.0.0")
            }
        )
        assert draft_build_version_to_invalidate(existing, updated) == "1.0.0"

    def test_source_version_bump_required_views(self):
        """LANDMINE 2 guard: any views change must bump a deployed version."""
        base = {
            "date_time": "2026-01-01",
            "match": {"field": "f", "value": "v"},
            "schema": {},
        }
        prev = SourceVersion.model_validate(base)

        # Standard set change bumps.
        added = SourceVersion.model_validate(
            {**base, "views": [{"standard": "sigma", "taxonomy": "windows"}]}
        )
        assert source_version_bump_required(prev, added) is True

        # Per-view custom_mappings change bumps.
        remapped = SourceVersion.model_validate(
            {
                **base,
                "views": [
                    {
                        "standard": "sigma",
                        "taxonomy": "windows",
                        "custom_mappings": {"User": "user_name"},
                    }
                ],
            }
        )
        assert source_version_bump_required(added, remapped) is True

        # Sigma logsource facet change bumps.
        rebound = SourceVersion.model_validate(
            {
                **base,
                "views": [{"standard": "sigma", "taxonomy": "windows", "service": "sysmon"}],
            }
        )
        assert source_version_bump_required(added, rebound) is True

        # Identical views do not bump.
        same = SourceVersion.model_validate(
            {**base, "views": [{"standard": "sigma", "taxonomy": "windows"}]}
        )
        assert source_version_bump_required(added, same) is False


class TestFlowFieldsDoNotBump:
    """A version id pins the shape of a table, so routing changes must not bump it."""

    BASE = {
        "date_time": "2026-01-01",
        "match": {"field": "f", "value": "v"},
        "schema": {},
        "transform": {"engine": "vrl"},
    }

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param({"transport": "direct"}, id="transport"),
            pytest.param({"archive": True}, id="archive"),
            pytest.param({"transform": {"engine": "vrl", "variant": "okta"}}, id="variant"),
        ],
    )
    def test_routing_change_does_not_bump(self, change):
        prev = SourceVersion.model_validate(self.BASE)
        updated = SourceVersion.model_validate({**self.BASE, **change})

        assert source_version_bump_required(prev, updated) is False

    def test_a_fetcher_route_does_not_bump(self):
        base = {"date_time": "2026-01-01", "fetcher": {"source_type": "okta"}, "schema": {}}
        prev = SourceVersion.model_validate(base)
        routed = SourceVersion.model_validate(
            {
                **base,
                "fetcher": {
                    "source_type": "okta",
                    "routes": [{"match": {"field": "eventType", "value": "x"}, "source": "other"}],
                },
            }
        )

        assert source_version_bump_required(prev, routed) is False

    def test_swapping_the_transform_engine_still_bumps(self):
        prev = SourceVersion.model_validate(self.BASE)
        swapped = SourceVersion.model_validate({**self.BASE, "transform": {"engine": "vector"}})

        assert source_version_bump_required(prev, swapped) is True


class TestFlowFields:
    def test_transport_and_archive_default_to_the_deployment_and_off(self):
        source = Source.model_validate(
            {"source": "auth", "match": {"field": "_source", "value": "auth"}}
        )

        assert source.transport is None
        assert source.archive is False

    def test_flow_fields_round_trip_through_a_write(self):
        write = SourceWriteRequest.model_validate(
            {
                "source": "auth",
                "match": {"field": "_source", "value": "auth"},
                "transport": "direct",
                "archive": False,
                "transform": {"engine": "vrl", "variant": "okta_system"},
            }
        )
        snapshot = write.to_version_snapshot()

        assert snapshot.transport == "direct"
        assert snapshot.archive is False
        assert snapshot.transform is not None
        assert snapshot.transform.variant == "okta_system"

    def test_a_put_that_omits_them_keeps_the_flow_the_source_already_has(self):
        """A description edit is not a request to move the source off direct."""
        existing = Source.model_validate(
            {
                "source": "auth",
                "match": {"field": "_source", "value": "auth"},
                "transport": "direct",
                "archive": True,
            }
        )
        write = SourceWriteRequest.model_validate(
            {"description": "doc-only edit", "match": {"field": "_source", "value": "auth"}}
        )

        updated = apply_source_write_update(existing, write)

        assert updated.transport == "direct"
        assert updated.archive is True
        assert updated.description == "doc-only edit"

    def test_a_put_that_sends_them_still_turns_them_off(self):
        """Inheritance is on ABSENCE, so neither field becomes one-way."""
        existing = Source.model_validate(
            {
                "source": "auth",
                "match": {"field": "_source", "value": "auth"},
                "transport": "direct",
                "archive": True,
            }
        )
        write = SourceWriteRequest.model_validate(
            {
                "match": {"field": "_source", "value": "auth"},
                "transport": None,
                "archive": False,
            }
        )

        updated = apply_source_write_update(existing, write)

        assert updated.transport is None
        assert updated.archive is False

    def test_a_legacy_flat_document_carries_the_flow_fields_into_its_version(self):
        source = Source.model_validate(
            {
                "source": "auth",
                "match": {"field": "_source", "value": "auth"},
                "transport": "bus",
                "archive": True,
            }
        )

        assert source.versions["1.0.0"].transport == "bus"
        assert source.archive is True

    def test_always_needs_no_operand_and_every_other_operator_does(self):
        assert SourceMatch(field="_source", operator="always").value == ""

        with pytest.raises(ValueError, match=r"match\.value is required"):
            SourceMatch(field="_source", operator="equals")

    def test_a_fetcher_route_validates_the_source_it_names(self):
        with pytest.raises(ValueError, match="DNS-1123 label"):
            FetcherRoute.model_validate(
                {"match": {"field": "f", "value": "v"}, "source": "Not_A_Label"}
            )


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
            version=SourceVersion(
                date_time="2026-06-12",
                header=SourceHeader(type="timeseries", version="1.0.0"),
                schema_config=SourceSchema(engine="MergeTree"),
                match=SourceMatch(field="ingest_type", value="x"),
                transform=SourceTransform(engine="vector"),
            ),
        )
        assert resp.source == "my-source"
        assert resp.version.schema_config.engine == "MergeTree"
        assert resp.version.match is not None
        assert resp.version.match.value == "x"


class TestPaginatedSourceSummaryResponse:
    def test_from_summaries_pagination_and_tree(self):
        objs = [
            SourceSummaryObject(
                name="aws-cloudtrail",
                current="1.0.0",
                deployed_version="1.0.0",
                versions=["1.0.0"],
            ),
            SourceSummaryObject(
                name="syslog",
                current="1.0.0",
                deployed_version="1.0.0",
                versions=["1.0.0"],
            ),
        ]
        resp = PaginatedSourceSummaryResponse.from_summaries(objs, page=1, per_page=1)
        assert resp.total == 2
        assert len(resp.items) == 1
        root_names = {obj.name for obj in resp.objects.items}
        assert root_names == {"aws-cloudtrail", "syslog"}
        assert resp.objects.children == {}


class TestSourceVersioning:
    def test_versioned_input_migrates_top_level_match_and_transform(self):
        ver_body = {
            "date_time": "2026-01-01",
            "header": {"type": "timeseries", "version": "1.0.0"},
            "match": {"field": "f", "value": "v"},
            "schema": {},
        }
        s = Source.model_validate(
            {
                "source": "legacy-top",
                "current": "1.0.0",
                "deployed_version": "1.0.0",
                "match": {"field": "f", "value": "v"},
                "transform": {"engine": "vector"},
                "versions": {"1.0.0": ver_body},
            }
        )
        assert s.versions["1.0.0"].match is not None
        assert s.versions["1.0.0"].match.value == "v"
        assert s.versions["1.0.0"].transform is not None
        out = s.to_yaml_dict()
        assert "match" not in out
        assert out["versions"]["1.0.0"]["match"]["value"] == "v"

    def test_versioned_yaml_shape(self):
        data = {
            "source": "no-transform",
            "display_name": "No Transform",
            "enabled": True,
            "deployed_version": "1.0.0",
            "current": "1.0.0",
            "versions": {
                "1.0.0": {
                    "date_time": "2026-06-10",
                    "header": {"type": "timeseries", "version": "1.0.0"},
                    "match": {"field": "f", "value": "v"},
                    "schema": {
                        "meta_schema": "meta/aws/cloudwatch_logs",
                        "meta_schema_version": "1.0.0",
                        "engine": "MergeTree",
                    },
                    "views": [
                        {"standard": "ecs", "field_map": "ecs/no_transform"},
                        {"standard": "sigma", "field_map": "sigma/no_transform"},
                    ],
                }
            },
        }
        s = Source.model_validate(data)
        out = s.to_yaml_dict()
        assert out["deployed_version"] == "1.0.0"
        assert out["versions"]["1.0.0"]["date_time"] == "2026-06-10"
        assert out["versions"]["1.0.0"]["schema"]["meta_schema"] == "meta/aws/cloudwatch_logs"
        assert s.fetcher is None

    def test_legacy_flat_input_normalizes_to_versions(self):
        s = Source.model_validate(
            {"source": "syslog", "match": {"field": "f", "value": "v"}, "schema": {"ttl_days": 90}}
        )
        assert "1.0.0" in s.versions
        assert s.schema_config.ttl_days == 90
        assert s.current == "1.0.0"
        assert s.deployed_version is None

    def test_legacy_flat_schema_config_key(self):
        s = Source.model_validate(
            {
                "source": "syslog",
                "match": {"field": "f", "value": "v"},
                "schema_config": {"ttl_days": 45, "engine": "MergeTree"},
            }
        )
        assert s.schema_config.ttl_days == 45

    def test_legacy_flat_moves_versioned_keys_into_snapshot(self):
        """Guards _VERSIONED_KEYS: flat bodies normalize into the version tree."""
        s = Source.model_validate(
            {
                "source": "pull-src",
                "fetcher": {"source_type": "m365", "config": {"services": [{"name": "alerts"}]}},
                "views": [{"standard": "sigma", "taxonomy": "windows"}],
            }
        )
        ver = s.versions["1.0.0"]
        assert ver.fetcher is not None
        assert ver.fetcher.config == {"services": [{"name": "alerts"}]}
        assert ver.origin == "fetcher"
        assert ver.view_for("sigma") is not None
        assert ver.view_for("sigma").taxonomy == "windows"

    def test_versioned_input_defaults_current_from_deployed_only(self):
        ver_body = {
            "date_time": "2026-01-01",
            "header": {"type": "timeseries", "version": "1.0.0"},
            "match": {"field": "f", "value": "v"},
            "schema": {},
        }
        s = Source.model_validate(
            {
                "source": "x",
                "deployed_version": "1.0.0",
                "versions": {"1.0.0": ver_body},
            }
        )
        assert s.current == "1.0.0"
        assert s.deployed_version == "1.0.0"

    def test_versioned_input_without_deployed_leaves_deployed_unset(self):
        ver_body = {
            "date_time": "2026-01-01",
            "header": {"type": "timeseries", "version": "2.0.0"},
            "match": {"field": "f", "value": "v"},
            "schema": {},
        }
        s = Source.model_validate(
            {
                "source": "x",
                "current": "2.0.0",
                "versions": {"2.0.0": ver_body},
            }
        )
        assert s.current == "2.0.0"
        assert s.deployed_version is None

    def test_before_validator_passthrough_non_dict(self):
        with pytest.raises(Exception):
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
            Source._validate_versions_and_display_name(src)

    def test_current_not_in_versions(self):
        ver_body = {
            "date_time": "2026-01-01",
            "header": {"type": "timeseries", "version": "1.0.0"},
            "match": {"field": "f", "value": "v"},
            "schema": {},
        }
        with pytest.raises(ValueError, match=r"current version '2\.0\.0'"):
            Source.model_validate(
                {
                    "source": "x",
                    "current": "2.0.0",
                    "deployed_version": "1.0.0",
                    "versions": {"1.0.0": ver_body},
                }
            )

    def test_deployed_version_not_in_versions(self):
        ver_body = {
            "date_time": "2026-01-01",
            "header": {"type": "timeseries", "version": "1.0.0"},
            "match": {"field": "f", "value": "v"},
            "schema": {},
        }
        with pytest.raises(ValueError, match=r"deployed_version '9\.9\.9'"):
            Source.model_validate(
                {
                    "source": "x",
                    "current": "1.0.0",
                    "deployed_version": "9.9.9",
                    "versions": {"1.0.0": ver_body},
                }
            )

    def test_version_lookup_unknown_id(self):
        s = Source.model_validate({"source": "x", "match": {"field": "f", "value": "v"}})
        with pytest.raises(ValueError, match=r"Source version '9\.9\.9' is not defined"):
            s.version("9.9.9")

    def test_to_yaml_dict_includes_transform(self):
        s = Source.model_validate(
            {
                "source": "with-xform",
                "match": {"field": "f", "value": "v"},
                "transform": {"engine": "vector", "config_file": "/etc/vector/x.yaml"},
            }
        )
        out = s.to_yaml_dict()
        ver = out["versions"]["1.0.0"]
        assert ver["transform"]["engine"] == "vector"
        assert ver["transform"]["config_file"] == "/etc/vector/x.yaml"
