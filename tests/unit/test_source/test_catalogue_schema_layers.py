#  Project:      dfe-engine
#  File:         tests/unit/test_source/test_catalogue_schema_layers.py
#  Purpose:      Which schema files a source created from a catalogue entry builds its table from
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The schema layers a catalogue source binds, and the columns its build reads from them.

Each case lays out a schemas tree, and a deploy repo where gitops is on, then
asserts the write body the entry compiles to. Where a case is about where a
file resolves, it also builds the table from that body, because a reference
the build cannot read is the failure binding it is meant to prevent.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from dfe_engine.appmgmt.catalogue import AppDescriptor, load_catalogue
from dfe_engine.schema.derived_registry import derived_reference_root
from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2
from dfe_engine.settings import DFESettings, GitopsSettings, SchemasSettings
from dfe_engine.source import catalogue as sc
from dfe_engine.source.models import SourceWriteRequest, source_from_write
from dfe_engine.yaml_utils import yaml_dump

SHIPPED = Path(__file__).parents[2] / "fixtures" / "catalogue" / "sources.yaml"

ECS = "meta/elastic/ecs"
DEPLOY_SCHEMAS = Path("config") / "schemas"


def _columns(*names: str) -> dict:
    """A one-version meta or additional document declaring string columns."""
    columns = [{"name": name, "type": "string", "use_case": "exact_match"} for name in names]
    return {
        "current": "1.0.0",
        "versions": {"1.0.0": {"date": "2026-09-21", "type": "model", "columns": columns}},
    }


def _selection(base: str, *names: str) -> dict:
    """A one-version derived document selecting *names* from *base*."""
    return {
        "base": base,
        "base_version": "1.0.0",
        "current": "1.0.0",
        "versions": {"1.0.0": {"date": "2026-09-21", "select": [{"name": name} for name in names]}},
    }


def _put(root: Path, reference: str, doc: dict) -> None:
    path = root / f"{reference}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml_dump(doc, path)


def _tree_settings(schemas: Path, deploy_repo: Path | None = None) -> DFESettings:
    """Settings reading *schemas*, with gitops on over *deploy_repo* when one is given."""
    gitops = GitopsSettings(enabled=deploy_repo is not None, local_path=str(deploy_repo or ""))
    return DFESettings(env="dev", schemas=SchemasSettings(schemas_dir=str(schemas)), gitops=gitops)


def _elastic_declaring(tmp_path: Path, **catalogue: str) -> AppDescriptor:
    """The elastic app as a manifest declaring *catalogue* beside the file conventions reads it."""
    block = {
        "file": "sources.yaml",
        "entries_key": "sources",
        "variant_pattern": "filebeat.{entry}.{transform}",
        **catalogue,
    }
    path = tmp_path / "apps.yaml"
    yaml_dump({"apps": {"dfe-transform-elastic": {"catalogue": block}}}, path)
    return load_catalogue(path)["dfe-transform-elastic"]


def _layers(write: SourceWriteRequest) -> tuple[str | None, str | None, str | None] | None:
    schema = write.schema_config
    if schema is None:
        return None
    return schema.meta_schema, schema.derived_schema, schema.additional_fields


def _built_columns(write: SourceWriteRequest, settings: DFESettings) -> list[str]:
    """The column names the table build composes from *write*, read the way a deploy reads them."""
    builder = SchemaBuilderV2(
        schemas_base_dir=settings.schemas.schemas_dir,
        derived_base_dir=derived_reference_root(settings),
    )
    assert write.source is not None
    source = source_from_write(write, source_name=write.source)
    return [column.name for column in builder.load_columns_for_source_version(source)]


@pytest.fixture(scope="module")
def shipped() -> sc.SourceCatalogue:
    """The vendored copy of the catalogue dfe-transform-elastic publishes."""
    catalogue = sc.load_source_catalogue(SHIPPED)
    assert catalogue is not None
    return catalogue


def _compile(
    catalogue: sc.SourceCatalogue, entry: str, settings: DFESettings
) -> SourceWriteRequest:
    intake = "fetcher" if entry == "aws_cloudtrail" else "receiver"
    return sc.write_request_for(catalogue, catalogue.entry(entry), intake=intake, settings=settings)


class TestTheManifestNamesTheLayers:
    def test_the_layers_bound_are_the_ones_the_manifest_names(self, shipped, tmp_path):
        schemas = tmp_path / "schemas"
        _put(schemas, "meta/acme/flat", _columns("event_action", "host_name"))
        _put(schemas, "derived/acme/cisco_ios-log", _selection("meta/acme/flat", "event_action"))
        _put(schemas, "vendor/acme/cisco_ios-log", _columns("cisco_ios_facility"))
        # The ECS layout sits beside them, so a binding that ignored the manifest would find it.
        _put(schemas, ECS, _columns("event_action"))
        _put(schemas, "derived/cisco_ios/log", _selection(ECS, "event_action"))
        app = _elastic_declaring(
            tmp_path,
            meta_schema="meta/acme/flat",
            derived_pattern="derived/acme/{package}-{data_stream}",
            additional_pattern="vendor/acme/{package}-{data_stream}",
        )

        write = _compile(replace(shipped, app=app), "cisco_ios", _tree_settings(schemas))

        assert _layers(write) == (
            "meta/acme/flat",
            "derived/acme/cisco_ios-log",
            "vendor/acme/cisco_ios-log",
        )

    def test_the_shipped_manifest_binds_ecs_and_both_layers_for_the_stream(self, shipped, tmp_path):
        _put(tmp_path, ECS, _columns("event_action", "host_name"))
        _put(tmp_path, "derived/cisco_ios/log", _selection(ECS, "event_action"))
        _put(tmp_path, "additional/cisco_ios/log", _columns("cisco_ios_facility"))

        write = _compile(shipped, "cisco_ios", _tree_settings(tmp_path))

        assert _layers(write) == (ECS, "derived/cisco_ios/log", "additional/cisco_ios/log")

    def test_a_manifest_naming_no_layers_binds_no_schema(self, shipped, tmp_path):
        _put(tmp_path, ECS, _columns("event_action"))
        _put(tmp_path, "derived/cisco_ios/log", _selection(ECS, "event_action"))
        app = _elastic_declaring(tmp_path)

        write = _compile(replace(shipped, app=app), "cisco_ios", _tree_settings(tmp_path))

        assert write.schema_config is None


class TestAllOrNothing:
    def test_with_no_derived_schema_nothing_is_bound(self, shipped, tmp_path):
        _put(tmp_path, ECS, _columns("event_action"))

        write = _compile(shipped, "fortinet", _tree_settings(tmp_path))

        assert write.schema_config is None

    def test_vendor_fields_without_a_derived_schema_bind_nothing(self, shipped, tmp_path):
        _put(tmp_path, ECS, _columns("event_action"))
        _put(tmp_path, "additional/fortinet_fortigate/log", _columns("fortinet_firewall_action"))

        write = _compile(shipped, "fortinet", _tree_settings(tmp_path))

        assert write.schema_config is None

    def test_a_derived_schema_over_another_base_binds_nothing(self, shipped, tmp_path):
        _put(tmp_path, ECS, _columns("event_action"))
        _put(tmp_path, "meta/aws/cloudtrail", _columns("EventName"))
        _put(tmp_path, "derived/aws/cloudtrail", _selection("meta/aws/cloudtrail", "EventName"))
        _put(tmp_path, "additional/aws/cloudtrail", _columns("aws_cloudtrail_flattened"))

        write = _compile(shipped, "aws_cloudtrail", _tree_settings(tmp_path))

        assert write.schema_config is None

    def test_a_derived_schema_that_does_not_read_binds_nothing(self, shipped, tmp_path):
        # A suffixed base is refused by the derived-schema model.
        _put(tmp_path, ECS, _columns("event_action"))
        _put(tmp_path, "derived/cisco_ios/log", _selection(f"{ECS}.yaml", "event_action"))

        write = _compile(shipped, "cisco_ios", _tree_settings(tmp_path))

        assert write.schema_config is None

    def test_nothing_is_bound_until_the_meta_schema_ships(self, shipped, tmp_path):
        _put(tmp_path, "derived/cisco_ios/log", _selection(ECS, "event_action"))
        _put(tmp_path, "additional/cisco_ios/log", _columns("cisco_ios_facility"))

        write = _compile(shipped, "cisco_ios", _tree_settings(tmp_path))

        assert write.schema_config is None

    def test_vendor_fields_not_shipped_for_the_stream_leave_the_other_two_bound(
        self, shipped, tmp_path
    ):
        _put(tmp_path, ECS, _columns("event_action"))
        _put(tmp_path, "derived/cisco_ios/log", _selection(ECS, "event_action"))

        write = _compile(shipped, "cisco_ios", _tree_settings(tmp_path))

        assert _layers(write) == (ECS, "derived/cisco_ios/log", None)

    def test_the_vendor_meta_schema_is_never_bound_to_transformed_output(self, shipped, tmp_path):
        # meta/aws/cloudtrail is the raw CloudTrail API shape; the transform emits ECS.
        _put(tmp_path, ECS, _columns("event_action"))
        _put(tmp_path, "meta/aws/cloudtrail", _columns("EventName"))
        _put(tmp_path, "derived/aws/cloudtrail", _selection(ECS, "event_action"))

        write = _compile(shipped, "aws_cloudtrail", _tree_settings(tmp_path))

        assert _layers(write) == (ECS, "derived/aws/cloudtrail", None)


class TestWhereTheDerivedSchemaResolves:
    def test_the_bound_layers_compose_narrowed_then_appended(self, shipped, tmp_path):
        _put(tmp_path, ECS, _columns("event_action", "host_name", "source_ip"))
        _put(tmp_path, "derived/cisco_ios/log", _selection(ECS, "source_ip", "event_action"))
        _put(tmp_path, "additional/cisco_ios/log", _columns("cisco_ios_facility"))
        settings = _tree_settings(tmp_path)

        names = _built_columns(_compile(shipped, "cisco_ios", settings), settings)

        assert names[-3:] == ["source_ip", "event_action", "cisco_ios_facility"]
        assert "host_name" not in names

    def test_with_gitops_on_a_derived_schema_only_the_release_ships_is_bound_and_built(
        self, shipped, tmp_path
    ):
        schemas = tmp_path / "schemas"
        _put(schemas, ECS, _columns("event_action", "host_name", "source_ip"))
        _put(schemas, "derived/cisco_ios/log", _selection(ECS, "source_ip", "event_action"))
        _put(schemas, "additional/cisco_ios/log", _columns("cisco_ios_facility"))
        settings = _tree_settings(schemas, deploy_repo=tmp_path / "deploy")

        write = _compile(shipped, "cisco_ios", settings)

        assert _layers(write) == (ECS, "derived/cisco_ios/log", "additional/cisco_ios/log")
        names = _built_columns(write, settings)
        assert names[-3:] == ["source_ip", "event_action", "cisco_ios_facility"]
        assert "host_name" not in names

    def test_with_gitops_on_the_deploy_repo_copy_wins_over_the_shipped_one(self, shipped, tmp_path):
        schemas = tmp_path / "schemas"
        deploy = tmp_path / "deploy"
        _put(schemas, ECS, _columns("event_action", "host_name"))
        _put(schemas, "derived/cisco_ios/log", _selection(ECS, "host_name"))
        _put(deploy / DEPLOY_SCHEMAS, "derived/cisco_ios/log", _selection(ECS, "event_action"))
        settings = _tree_settings(schemas, deploy_repo=deploy)

        write = _compile(shipped, "cisco_ios", settings)

        assert _layers(write) == (ECS, "derived/cisco_ios/log", None)
        names = _built_columns(write, settings)
        assert names[-1] == "event_action"
        assert "host_name" not in names

    def test_a_deploy_repo_copy_over_another_base_binds_nothing_though_the_release_ships_one(
        self, shipped, tmp_path
    ):
        # The deployment's own copy is the one the build reads, so it decides.
        schemas = tmp_path / "schemas"
        deploy = tmp_path / "deploy"
        _put(schemas, ECS, _columns("event_action"))
        _put(schemas, "derived/cisco_ios/log", _selection(ECS, "event_action"))
        _put(
            deploy / DEPLOY_SCHEMAS,
            "derived/cisco_ios/log",
            _selection("meta/cisco/ios", "message"),
        )

        write = _compile(shipped, "cisco_ios", _tree_settings(schemas, deploy_repo=deploy))

        assert write.schema_config is None
