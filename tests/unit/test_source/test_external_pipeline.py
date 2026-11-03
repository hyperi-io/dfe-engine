#  Project:      dfe-engine
#  File:         tests/unit/test_source/test_external_pipeline.py
#  Purpose:      A customer's own pipeline as a source's origin or transform, on the bus
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What an external origin or transform saves as, resolves to, and is refused for.

The values asserted are the ones a customer's pipeline is told to use - the topic
pair, the consumer group, the loader's hold on the topic - because a wrong one
there strands records in a topic nothing reads.
"""

from contextlib import nullcontext

import pytest
from pydantic import ValidationError

from dfe_engine.api.v1.sources import _connection_model, _writer_groups
from dfe_engine.appmgmt.routing import RoutingNotApplicableError, compile_for
from dfe_engine.kafka.topics import source_topic_names, source_topic_specs
from dfe_engine.orgs.models import Org
from dfe_engine.orgs.tenancy import Tenancy
from dfe_engine.services.source_routing import compile_loader_routing, version_rules
from dfe_engine.settings import DFESettings
from dfe_engine.source.flow import FlowError, resolve_flow
from dfe_engine.source.models import Source, SourceTransform, SourceWriteRequest
from tests.unit.test_services.test_source_routing import FakeSourceRegistry

TENANCY = Tenancy(
    orgs=(Org(name="default", org_ids=["default"]), Org(name="acme", org_ids=["acme-prod"]))
)


def _settings(**transport) -> DFESettings:
    return DFESettings(
        env="dev",
        transport={"default": "bus", **transport},
        kafka={
            "bootstrap_servers": "kafka:9092",
            "external_bootstrap_servers": "kafka.example:9094",
        },
    )


def _origin(name: str = "acme-logs", transport: str | None = None, **external) -> Source:
    return Source.model_validate({"source": name, "transport": transport, "external": external})


def _transformed(name: str = "acme-logs", transport: str | None = None, **transform) -> Source:
    return Source.model_validate(
        {
            "source": name,
            "transport": transport,
            "match": {"field": "_source", "value": name},
            "transform": {"engine": "external", **transform},
        }
    )


@pytest.mark.parametrize(
    ("transform", "error"),
    [
        pytest.param({"engine": "external"}, None, id="external"),
        pytest.param(
            {"engine": "external", "credential_ref": "vault:kv/acme/kafka"}, None, id="reference"
        ),
        pytest.param(
            {"engine": "external", "org": "acme", "allowed_org_ids": ["acme-eu", "acme-us"]},
            None,
            id="allow-list",
        ),
        pytest.param({"engine": "external", "variant": "okta"}, "variant", id="variant"),
        pytest.param({"engine": "external", "mode": "agent"}, "mode", id="mode"),
        pytest.param({"engine": "external", "env": {"A": "1"}}, "env", id="env"),
        pytest.param({"engine": "external", "files": ["geo.mmdb"]}, "files", id="files"),
        pytest.param({"engine": "external", "config_file": "x.toml"}, "config_file", id="config"),
        pytest.param(
            {"engine": "external", "credential_ref": "hunter2"},
            "never the credential",
            id="literal",
        ),
        pytest.param({"engine": "vrl", "credential_ref": "vault:kv/x"}, "itself", id="vrl-ref"),
        pytest.param({"engine": "vrl", "org": "acme"}, "itself", id="vrl-org"),
        pytest.param(
            {"engine": "external", "allowed_org_ids": ["a,b"]}, "not a tenant id", id="comma"
        ),
        pytest.param(
            {"engine": "external", "allowed_org_ids": ["acme", "acme"]},
            "more than once",
            id="dup-org",
        ),
        pytest.param(
            {"engine": "external", "allowed_sources": ["Bad_Name"]}, "DNS-1123", id="bad-source"
        ),
        pytest.param(
            {"engine": "external", "allowed_sources": ["a", "a"]}, "more than once", id="dup-source"
        ),
        pytest.param({"engine": "external", "org": "has space"}, "pattern", id="bad-org"),
        pytest.param({"engine": "nope"}, "Invalid transform engine", id="unknown"),
    ],
)
def test_the_transform_model(transform, error):
    with pytest.raises(ValidationError, match=error) if error else nullcontext():
        SourceTransform.model_validate(transform)


@pytest.mark.parametrize(
    ("body", "error"),
    [
        pytest.param({"external": {}}, None, id="external-origin"),
        pytest.param(
            {"external": {}, "transform": {"engine": "vrl"}}, None, id="origin-then-dfe-transform"
        ),
        pytest.param(
            {"external": {}, "match": {"field": "a", "value": "b"}}, "exactly one", id="two-origins"
        ),
        pytest.param({}, "exactly one", id="no-origin"),
        pytest.param(
            {"external": {}, "transform": {"engine": "external"}},
            "two of the customer's pipelines",
            id="external-twice",
        ),
    ],
)
def test_a_write_declares_one_origin_and_one_external_pipeline(body, error):
    with pytest.raises(ValidationError, match=error) if error else nullcontext():
        SourceWriteRequest.model_validate({"source": "acme-logs", **body})


def test_the_stored_source_refuses_the_customer_pipeline_twice():
    with pytest.raises(ValidationError, match="two of the customer's pipelines"):
        Source.model_validate(
            {"source": "acme-logs", "external": {}, "transform": {"engine": "external"}}
        )


class TestFlow:
    def test_an_external_transform_deploys_nothing_and_names_its_group(self):
        flow = resolve_flow(_transformed(), _settings())

        assert flow.transform is not None
        assert (flow.transform.engine, flow.transform.app, flow.transform.instance) == (
            "external",
            None,
            None,
        )
        assert flow.transform.topics == ("acme-logs_land", "acme-logs_load")
        assert flow.transform.group == "dfe-external-acme-logs"
        assert flow.outputs.loader == "acme-logs_load"

    def test_an_external_origin_reads_its_landing_topic(self):
        flow = resolve_flow(_origin(), _settings())

        assert (flow.origin, flow.input, flow.outputs.loader) == (
            "external",
            "acme-logs_land",
            "acme-logs_land",
        )

    def test_a_dfe_transform_names_the_group_it_reads_in(self):
        source = Source.model_validate(
            {
                "source": "auth",
                "match": {"field": "_source", "value": "auth"},
                "transform": {"engine": "vrl"},
            }
        )
        flow = resolve_flow(source, _settings())

        assert flow.transform is not None
        assert flow.transform.group == "dfe-transform-vrl-auth"

    @pytest.mark.parametrize(
        "source", [_origin(transport="direct"), _transformed(transport="direct")]
    )
    def test_both_shapes_are_bus_only(self, source):
        with pytest.raises(FlowError, match="on the bus only"):
            resolve_flow(source, _settings(default="direct", bus_present=True))


def test_the_receiver_gets_no_row_for_an_external_origin():
    source = _origin()
    assert version_rules(source.source, source.landing_label(), source.version()) == []


class TestLoaderCompile:
    @pytest.mark.parametrize(
        ("source", "topic"),
        [
            pytest.param(
                _origin(org="acme", allowed_org_ids=["acme-eu"]), "acme-logs_land", id="origin"
            ),
            pytest.param(
                _transformed(org="acme", allowed_org_ids=["acme-eu"]),
                "acme-logs_load",
                id="transform",
            ),
        ],
    )
    def test_the_written_topic_is_held_to_the_source(self, source, topic):
        compiled = compile_loader_routing(FakeSourceRegistry([source]), tenancy=TENANCY)

        assert compiled.model_dump(mode="json", exclude_unset=True)["external_topics"] == [
            {
                "topic": topic,
                "org_id": "acme-prod",
                "allowed_org_ids": ["acme-eu"],
                "source": "acme-logs",
                "allowed_sources": [],
            }
        ]

    def test_no_org_is_the_default_org(self):
        compiled = compile_loader_routing(FakeSourceRegistry([_origin()]), tenancy=TENANCY)

        assert compiled.external_topics[0].org_id == "default"

    @pytest.mark.parametrize("tenancy", [TENANCY, None], ids=["unregistered", "no-tenancy"])
    def test_an_org_that_cannot_be_resolved_leaves_the_topic_out(self, tenancy):
        compiled = compile_loader_routing(
            FakeSourceRegistry([_origin(org="ghost")]), tenancy=tenancy
        )

        assert "external_topics" not in compiled.model_dump(mode="json", exclude_unset=True)

    def test_a_deployment_with_no_external_source_emits_nothing_new(self):
        receiver = Source.model_validate({"source": "auth", "match": {"field": "a", "value": "b"}})
        compiled = compile_loader_routing(FakeSourceRegistry([receiver]), tenancy=TENANCY)

        assert "external_topics" not in compiled.model_dump(mode="json", exclude_unset=True)


@pytest.mark.parametrize(
    ("source", "names"),
    [
        pytest.param(_origin(), ["acme-logs_land"], id="origin"),
        pytest.param(_transformed(), ["acme-logs_land", "acme-logs_load"], id="transform"),
    ],
)
def test_the_engine_creates_and_removes_both_topics(source, names):
    specs = source_topic_specs(source, partitions=3, replication_factor=1)

    assert [spec.name for spec in specs] == names
    assert source_topic_names(source) == names


def test_a_dropped_external_transform_is_judged_on_the_customers_group():
    assert _writer_groups(_transformed(), None) == ["dfe-external-acme-logs"]


def test_the_transform_compile_skips_an_external_transform():
    from dfe_engine.appmgmt import catalogue

    app = catalogue.descriptor("dfe-transform-vrl")
    with pytest.raises(RoutingNotApplicableError, match="customer's own pipeline"):
        compile_for(
            app,
            FakeSourceRegistry([_transformed()]),
            _settings(),
            instance="acme-logs",
            tenancy=TENANCY,
        )


class TestConnectionBlock:
    def test_a_transform_is_told_what_to_read_write_and_join(self):
        source = _transformed(credential_ref="vault:kv/acme/kafka", allowed_sources=["acme-audit"])
        block = _connection_model(source, _settings())

        assert block is not None
        assert (block.role, block.reads, block.writes, block.consumer_group) == (
            "transform",
            "acme-logs_land",
            "acme-logs_load",
            "dfe-external-acme-logs",
        )
        assert block.bootstrap == "kafka.example:9094"
        assert block.credential_ref == "vault:kv/acme/kafka"
        assert block.allowed_sources == ["acme-audit"]
        assert [field.field for field in block.header_fields][:2] == ["_timestamp", "_org_id"]
        assert block.sample_record["_source"] == "acme-logs"

    def test_an_origin_writes_the_landing_topic_and_joins_no_group(self):
        block = _connection_model(_origin(), _settings())

        assert block is not None
        assert (block.role, block.reads, block.writes, block.consumer_group) == (
            "origin",
            None,
            "acme-logs_land",
            None,
        )

    def test_the_bootstrap_falls_back_to_the_deployments(self):
        settings = DFESettings(
            env="dev", transport={"default": "bus"}, kafka={"bootstrap_servers": "kafka:9092"}
        )

        block = _connection_model(_origin(), settings)

        assert block is not None
        assert block.bootstrap == "kafka:9092"

    def test_a_source_with_no_external_pipeline_has_no_block(self):
        receiver = Source.model_validate({"source": "auth", "match": {"field": "a", "value": "b"}})

        assert _connection_model(receiver, _settings()) is None
