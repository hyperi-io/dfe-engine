"""Tests for Source Pydantic models."""

import pytest

from dfe_engine.source.models import (
    SchemaColumn,
    Source,
    SourceFetcher,
    SourceHeader,
    SourceMatch,
    SourceSchema,
    SourceSigma,
    SourceTransform,
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

    def test_attribute_coercion_from_none(self):
        col = SchemaColumn(name="x", type="string", attribute=None)
        assert col.attribute == []

    def test_validate_valid_column(self, registry: TypeRegistry):
        col = SchemaColumn(name="x", type="string", use_case="dimension")
        errors = col.validate_against_registry(registry)
        assert errors == []

    def test_validate_invalid_use_case(self, registry: TypeRegistry):
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
            name="x", type="json",
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


# ---------------------------------------------------------------------------
# SourceSchema
# ---------------------------------------------------------------------------


class TestSourceSchema:
    def test_minimal(self):
        s = SourceSchema(ttl_days=90)
        assert s.ttl_days == 90
        assert s.engine == "MergeTree"
        assert s.meta_schema is None

    def test_full(self):
        s = SourceSchema(
            meta_schema="logs_beats_filebeat",
            meta_schema_version="1.0.0",
            derived_schema="filebeat/derived",
            additional_fields="filebeat/add",
            ttl_days=90,
            engine="ReplicatedMergeTree",
        )
        assert s.meta_schema == "logs_beats_filebeat"
        assert s.engine == "ReplicatedMergeTree"

    def test_invalid_engine(self):
        with pytest.raises(ValueError, match="Invalid engine"):
            SourceSchema(engine="InvalidEngine")


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
# SourceSigma
# ---------------------------------------------------------------------------


class TestSourceSigma:
    def test_defaults(self):
        s = SourceSigma()
        assert s.taxonomy is None
        assert s.custom_mappings == {}

    def test_with_mappings(self):
        s = SourceSigma(
            taxonomy="windows",
            custom_mappings={"CommandLine": "command_line"},
        )
        assert s.taxonomy == "windows"
        assert s.custom_mappings["CommandLine"] == "command_line"


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
        assert s.header.type == "time_series"
        assert s.schema_config.engine == "MergeTree"

    def test_full(self):
        s = Source.model_validate({
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
            "sigma": {
                "taxonomy": "linux",
                "custom_mappings": {"User": "user_name"},
            },
        })
        assert s.source == "filebeat"
        assert s.display_name == "Elastic Filebeat"
        assert s.schema_config.meta_schema == "logs_beats_filebeat"
        assert s.transform.engine == "vector"
        assert s.sigma.taxonomy == "linux"

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
        s = Source(source="crowdstrike_edr")
        assert s.display_name == "Crowdstrike Edr"

    def test_display_name_explicit(self):
        s = Source(source="crowdstrike_edr", display_name="CrowdStrike EDR")
        assert s.display_name == "CrowdStrike EDR"

    def test_mapping_standards_default_empty(self):
        s = Source(source="syslog")
        assert s.mapping_standards == []

    def test_mapping_standards_set(self):
        s = Source(source="syslog", mapping_standards=["sigma", "ecs"])
        assert s.mapping_standards == ["sigma", "ecs"]

    def test_mapping_standards_in_yaml_dict(self):
        s = Source(source="syslog", mapping_standards=["sigma"])
        d = s.to_yaml_dict()
        assert d["mapping_standards"] == ["sigma"]

    def test_mapping_standards_excluded_when_empty(self):
        s = Source(source="syslog")
        d = s.to_yaml_dict()
        assert "mapping_standards" not in d


# ---------------------------------------------------------------------------
# Source — Naming Validation
# ---------------------------------------------------------------------------


class TestSourceNaming:
    def test_valid_names(self):
        for name in ["filebeat", "syslog", "crowdstrike_edr", "a", "x123_456"]:
            s = Source(source=name)
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
        s = Source(source="a" * 64)
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
        assert yaml_dict["match"]["field"] == "tags.collector.type"
        assert yaml_dict["schema"]["ttl_days"] == 90
        assert "schema_config" not in yaml_dict  # Uses alias 'schema'

    def test_excludes_none(self):
        s = Source(source="syslog")
        yaml_dict = s.to_yaml_dict()
        assert "match" not in yaml_dict
        assert "transform" not in yaml_dict
        assert "fetcher" not in yaml_dict
        assert "sigma" not in yaml_dict
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
                "engine": "SharedMergeTree",
            },
        }
        s1 = Source.model_validate(data)
        yaml_dict = s1.to_yaml_dict()
        s2 = Source.model_validate(yaml_dict)

        assert s1.source == s2.source
        assert s1.display_name == s2.display_name
        assert s1.enabled == s2.enabled
        assert s1.schema_config.engine == s2.schema_config.engine
