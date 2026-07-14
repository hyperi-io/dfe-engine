"""Tests for Source Pydantic models."""

import pytest

from dfe_engine.source.models import (
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
        assert h.type == "time_series"
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

    def test_wasm(self):
        t = SourceTransform(engine="wasm")
        assert t.engine == "wasm"

    def test_invalid_engine(self):
        with pytest.raises(ValueError, match="Invalid transform engine"):
            SourceTransform(engine="spark")


# ---------------------------------------------------------------------------
# SourceFetcher
# ---------------------------------------------------------------------------


class TestSourceFetcher:
    def test_minimal(self):
        f = SourceFetcher(source_type="crowdstrike")
        assert f.source_type == "crowdstrike"
        assert f.poll_interval_secs == 300

    def test_with_auth(self):
        f = SourceFetcher(
            source_type="m365",
            base_url="https://graph.microsoft.com",
            auth={"type": "oauth2", "token_url": "https://login.microsoft.com/token"},
        )
        assert f.auth is not None
        assert f.auth.type == "oauth2"


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
                "header": {"type": "time_series", "version": "1.0.0"},
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
        s = Source(source="crowdstrike_edr", match=SourceMatch(field="f", value="v"))
        assert s.display_name == "Crowdstrike Edr"

    def test_display_name_explicit(self):
        s = Source(
            source="crowdstrike_edr",
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
    def test_valid_names(self):
        for name in ["filebeat", "syslog", "crowdstrike_edr", "a", "x123_456"]:
            s = Source(source=name, match=SourceMatch(field="f", value="v"))
            assert s.source == name

    def test_starts_with_digit(self):
        with pytest.raises(ValueError, match="must match"):
            Source(source="123abc")

    def test_uppercase(self):
        with pytest.raises(ValueError, match="must match"):
            Source(source="FileBeat")

    def test_hyphens(self):
        with pytest.raises(ValueError, match="must match"):
            Source(source="file-beat")

    def test_spaces(self):
        with pytest.raises(ValueError, match="must match"):
            Source(source="file beat")

    def test_too_long(self):
        with pytest.raises(ValueError, match="exceeds max length"):
            Source(source="a" * 65)

    def test_max_length_ok(self):
        s = Source(source="a" * 64, match=SourceMatch(field="f", value="v"))
        assert len(s.source) == 64

    def test_empty(self):
        with pytest.raises(ValueError):
            Source(source="")

    def test_underscore_only(self):
        with pytest.raises(ValueError, match="must match"):
            Source(source="_test")


# ---------------------------------------------------------------------------
# Source — YAML Serialisation
# ---------------------------------------------------------------------------


class TestSourceYaml:
    def test_round_trip(self):
        data = {
            "source": "filebeat",
            "match": {"field": "tags.collector.type", "value": "filebeat"},
            "header": {"type": "time_series", "version": "1.0.0"},
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
            "source": "test_source",
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
                "source": "dormant_src",
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
                "source": "profile_pin",
                "header": {"type": "common-header/minimal", "version": "1.1.0"},
                "match": {"field": "f", "value": "v"},
                "schema": {"engine": "MergeTree"},
            }
        )
        src = source_from_write(write, source_name="profile_pin")
        assert "1.0.0" in src.versions
        assert src.versions["1.0.0"].header.version == "1.1.0"
        assert src.current == "1.0.0"
        assert src.deployed_version is None

    def test_create_without_header_leaves_header_unset(self):
        write = SourceWriteRequest.model_validate(
            {
                "source": "no_header",
                "match": {"field": "f", "value": "v"},
                "schema": {"engine": "MergeTree"},
            }
        )
        src = source_from_write(write, source_name="no_header")
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
                "source": "src_a",
                "deployed_version": "1.0.0",
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "header": {"type": "time_series", "version": "1.0.0"},
                        "match": {"field": "f", "value": "v"},
                        "schema": {},
                    },
                    "2.0.0": {
                        "date_time": "2026-01-02",
                        "header": {"type": "time_series", "version": "1.0.0"},
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
                "source": "src_a",
                "deployed_version": "1.0.0",
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "header": {"type": "time_series", "version": "1.0.0"},
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
                "source": "src_a",
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
                "source": "src_a",
                "deployed_version": None,
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "header": {"type": "time_series", "version": "1.0.0"},
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
                "source": "src_a",
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
                "source": "src_a",
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
                "source": "src_a",
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
                "source": "src_a",
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


class TestSourceVersionGetResponse:
    def test_round_trip_fields(self):
        resp = SourceVersionGetResponse(
            source="my_source",
            display_name="My Source",
            description="desc",
            enabled=False,
            current="2.0.0",
            deployed_version="1.0.0",
            selected="1.0.0",
            versions=["1.0.0", "2.0.0"],
            version=SourceVersion(
                date_time="2026-06-12",
                header=SourceHeader(type="time_series", version="1.0.0"),
                schema_config=SourceSchema(engine="MergeTree"),
                match=SourceMatch(field="ingest_type", value="x"),
                transform=SourceTransform(engine="vector"),
            ),
        )
        assert resp.source == "my_source"
        assert resp.version.schema_config.engine == "MergeTree"
        assert resp.version.match is not None
        assert resp.version.match.value == "x"


class TestPaginatedSourceSummaryResponse:
    def test_from_summaries_pagination_and_tree(self):
        objs = [
            SourceSummaryObject(
                name="aws_cloudtrail",
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
        assert root_names == {"aws_cloudtrail", "syslog"}
        assert resp.objects.children == {}


class TestSourceVersioning:
    def test_versioned_input_migrates_top_level_match_and_transform(self):
        ver_body = {
            "date_time": "2026-01-01",
            "header": {"type": "time_series", "version": "1.0.0"},
            "match": {"field": "f", "value": "v"},
            "schema": {},
        }
        s = Source.model_validate(
            {
                "source": "legacy_top",
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
            "source": "no_transform",
            "display_name": "No Transform",
            "enabled": True,
            "deployed_version": "1.0.0",
            "current": "1.0.0",
            "versions": {
                "1.0.0": {
                    "date_time": "2026-06-10",
                    "header": {"type": "time_series", "version": "1.0.0"},
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
                    "fetcher": {
                        "source_type": "aws.cloudtrail",
                        "base_url": "https://{service}.{region}.amazonaws.com",
                        "poll_interval_secs": 10,
                    },
                }
            },
        }
        s = Source.model_validate(data)
        out = s.to_yaml_dict()
        assert out["deployed_version"] == "1.0.0"
        assert out["versions"]["1.0.0"]["date_time"] == "2026-06-10"
        assert out["versions"]["1.0.0"]["schema"]["meta_schema"] == "meta/aws/cloudwatch_logs"
        assert s.fetcher is not None
        assert s.fetcher.poll_interval_secs == 10

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
                "source": "pull_src",
                "match": {"field": "f", "value": "v"},
                "fetcher": {"source_type": "m365"},
                "views": [{"standard": "sigma", "taxonomy": "windows"}],
            }
        )
        ver = s.versions["1.0.0"]
        assert ver.fetcher is not None
        assert ver.view_for("sigma") is not None
        assert ver.view_for("sigma").taxonomy == "windows"

    def test_versioned_input_defaults_current_from_deployed_only(self):
        ver_body = {
            "date_time": "2026-01-01",
            "header": {"type": "time_series", "version": "1.0.0"},
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
            "header": {"type": "time_series", "version": "2.0.0"},
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
            source="empty_ver",
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
            "header": {"type": "time_series", "version": "1.0.0"},
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
            "header": {"type": "time_series", "version": "1.0.0"},
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
                "source": "with_xform",
                "match": {"field": "f", "value": "v"},
                "transform": {"engine": "vector", "config_file": "/etc/vector/x.yaml"},
            }
        )
        out = s.to_yaml_dict()
        ver = out["versions"]["1.0.0"]
        assert ver["transform"]["engine"] == "vector"
        assert ver["transform"]["config_file"] == "/etc/vector/x.yaml"
