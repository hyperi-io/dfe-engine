#  Project:      dfe-engine
#  File:         tests/unit/test_e2e_harness/test_flow_fixtures.py
#  Purpose:      The flow fixture loader, the transport rule and the skip policy
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The flow suite's own machinery, checked without a deployment.

The live suite cannot run in CI, so what CAN be checked here is everything that
decides what it runs and what it lets pass: that each committed fixture parses,
that a malformed one is refused rather than quietly running a weaker case, and
that the skip policy fails a run on any skip a fixture did not declare.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from dfe_engine.appmgmt import catalogue
from dfe_engine.settings import DFESettings
from dfe_engine.source.flow import FlowError, resolve_flow
from dfe_engine.source.models import Source
from tests.e2e.flows import shapes
from tests.e2e.flows.conftest import undeclared_skips

# What a deployment of the whole catalogue offers, so a case about the transport
# rule is not also making a statement about which apps are deployed.
EVERY_APP = tuple(catalogue.APP_CATALOGUE)


class TestTheCommittedFixtures:
    def test_every_shape_parses(self) -> None:
        loaded = shapes.load_shapes()
        assert {s.name for s in loaded} == {
            "archived",
            "catalogue",
            "default-flow",
            "fetcher-plain",
            "fetcher-transform",
            "multi-destination",
            "receiver-plain",
            "receiver-transform",
        }

    def test_every_shape_says_what_it_proves(self) -> None:
        for shape in shapes.load_shapes():
            assert shape.description, f"{shape.name}: expect.yaml carries no description"

    def test_a_transformed_source_ships_the_program_it_runs(self) -> None:
        for shape in shapes.load_shapes():
            if any("transform" in s for s in shape.sources):
                assert shape.transform_file, f"{shape.name}: no program for its transform"

    def test_a_transformed_source_expects_a_marker_only_the_program_sets(self) -> None:
        # Without one, an untransformed row satisfies the landing assertion on its
        # own and the transform hop is never actually proved.
        for shape in shapes.load_shapes():
            for declared in shape.sources:
                if "transform" not in declared:
                    continue
                expectation = next(e for e in shape.expect if e.source == declared["source"])
                assert expectation.marker, f"{shape.name}: {declared['source']} expects no marker"

    def test_every_created_source_names_a_meta_schema(self) -> None:
        # A source with no schema reference cannot be deployed: the engine refuses
        # POST /sources/{name}/deploy with no_schema, so the case never reaches
        # the flow it exists to prove.
        for shape in shapes.load_shapes():
            for declared in shape.sources:
                schema = declared.get("schema") or {}
                assert schema.get("meta_schema"), f"{shape.name}: {declared['source']}"
                assert schema.get("meta_schema_version"), f"{shape.name}: {declared['source']}"

    def test_no_match_rule_carries_the_json_column_prefix(self) -> None:
        # The receiver splits the match field on '.' and walks the raw payload, so
        # a '_json.' prefix is a first segment no record has and the rule is dead.
        for shape in shapes.load_shapes():
            for declared in shape.sources:
                field = (declared.get("match") or {}).get("field", "")
                assert not field.startswith("_json."), f"{shape.name}: {field}"


class TestAMalformedFixtureIsRefused:
    def _write(self, root: Path, name: str, source: dict, expect: dict) -> Path:
        directory = root / name
        directory.mkdir()
        (directory / "source.yaml").write_text(yaml.safe_dump(source), encoding="utf-8")
        (directory / "expect.yaml").write_text(yaml.safe_dump(expect), encoding="utf-8")
        return directory

    def test_a_receiver_shape_without_a_payload(self, tmp_path: Path) -> None:
        directory = self._write(
            tmp_path,
            "bad",
            {"sources": [{"source": "a", "match": {"field": "f", "value": "a"}}]},
            {"origin": "receiver", "expect": [{"source": "a", "table": "a"}]},
        )
        with pytest.raises(shapes.FixtureError, match=r"payload\.ndjson"):
            shapes.load_shape(directory)

    def test_a_fetcher_shape_that_posts_a_payload(self, tmp_path: Path) -> None:
        directory = self._write(
            tmp_path,
            "bad",
            {"sources": [{"source": "a", "fetcher": {"source_type": "crates_io"}}]},
            {"origin": "fetcher", "expect": [{"source": "a", "table": "a"}]},
        )
        (directory / "payload.ndjson").write_text('{"a": 1}\n', encoding="utf-8")
        with pytest.raises(shapes.FixtureError, match="posts nothing"):
            shapes.load_shape(directory)

    def test_an_expectation_for_a_source_nothing_creates(self, tmp_path: Path) -> None:
        directory = self._write(
            tmp_path,
            "bad",
            {"sources": [{"source": "a", "match": {"field": "f", "value": "a"}}]},
            {"origin": "receiver", "expect": [{"source": "b", "table": "b"}]},
        )
        (directory / "payload.ndjson").write_text('{"f": "a"}\n', encoding="utf-8")
        with pytest.raises(shapes.FixtureError, match=r"which source\.yaml does not create"):
            shapes.load_shape(directory)

    def test_a_transform_with_no_program(self, tmp_path: Path) -> None:
        directory = self._write(
            tmp_path,
            "bad",
            {
                "sources": [
                    {
                        "source": "a",
                        "match": {"field": "f", "value": "a"},
                        "transform": {"engine": "vrl"},
                    }
                ]
            },
            {"origin": "receiver", "expect": [{"source": "a", "table": "a"}]},
        )
        (directory / "payload.ndjson").write_text('{"f": "a"}\n', encoding="utf-8")
        with pytest.raises(shapes.FixtureError, match="must ship the program"):
            shapes.load_shape(directory)

    def test_a_skip_declared_against_a_transport_that_does_not_exist(self, tmp_path: Path) -> None:
        directory = self._write(
            tmp_path,
            "bad",
            {"sources": [{"source": "a", "match": {"field": "f", "value": "a"}}]},
            {
                "origin": "receiver",
                "expected_skip": {"grpc": "wrong vocabulary"},
                "expect": [{"source": "a", "table": "a"}],
            },
        )
        (directory / "payload.ndjson").write_text('{"f": "a"}\n', encoding="utf-8")
        with pytest.raises(shapes.FixtureError, match="unknown transport"):
            shapes.load_shape(directory)

    def test_an_empty_fixture_tree(self, tmp_path: Path) -> None:
        with pytest.raises(shapes.FixtureError, match="no flow fixtures"):
            shapes.load_shapes(tmp_path)


class TestTheTransportRule:
    def test_both_asks_for_each_transport_once(self) -> None:
        assert shapes.transports_for("both") == ("bus", "direct")

    def test_a_deployment_word_maps_to_one_source_word(self) -> None:
        assert shapes.transports_for("kafka") == ("bus",)
        assert shapes.transports_for("grpc") == ("direct",)

    def test_an_unknown_request_is_refused(self) -> None:
        with pytest.raises(ValueError, match="expected"):
            shapes.transports_for("rabbit")

    def test_the_archived_shape_runs_wherever_the_archiver_does(self) -> None:
        # What an app carries is apps.yaml data, so the fixture follows the manifest
        # rather than carrying its own opinion of the archiver's transports.
        archiver = catalogue.APP_CATALOGUE["dfe-archiver"]
        archived = next(s for s in shapes.load_shapes() if s.name == "archived")
        for transport in shapes.transports_for("both"):
            carries = archiver.carries(transport)
            # A deployment that carries this transport, so the archiver is the
            # only thing left that can refuse the shape.
            assert (archived.skip_reason(transport) is None) is carries
            assert (archived.refusal(transport, (transport,), EVERY_APP) is None) is carries

    def test_a_transport_the_deployment_does_not_carry_refuses_every_shape(self) -> None:
        # The bus profiles bind the loader to the topic, so nothing serves a direct
        # source there whatever the shape asks for.
        for shape in shapes.load_shapes():
            assert shape.refusal("direct", ("bus",), EVERY_APP) == "asks for the direct transport"
            assert shape.refusal("bus", ("direct",), EVERY_APP) == "asks for the bus transport"

    def test_a_shape_the_deployment_can_run_is_not_refused(self) -> None:
        plain = next(s for s in shapes.load_shapes() if s.name == "receiver-plain")

        for transport in shapes.transports_for("both"):
            assert plain.refusal(transport, (transport,), EVERY_APP) is None

    def test_the_catalogue_shape_runs_wherever_the_app_shipping_it_does(self) -> None:
        # Every entry compiles onto the transform of the app that ships the
        # catalogue, so the shape can only run where that app can.
        shipper = catalogue.APP_CATALOGUE["dfe-transform-elastic"]
        entry = next(s for s in shapes.load_shapes() if s.name == "catalogue")
        for transport in shapes.transports_for("both"):
            assert (entry.skip_reason(transport) is None) is shipper.carries(transport)


class TestAnAppTheDeploymentDoesNotDeploy:
    def _without(self, service: str) -> tuple[str, ...]:
        return tuple(a for a in EVERY_APP if a != service)

    def test_a_fetcher_shape_is_refused_where_no_fetcher_is_deployed(self) -> None:
        fetchers = [s for s in shapes.load_shapes() if s.origin == "fetcher"]
        assert fetchers, "no fetcher-origin shape left to check"
        offered = self._without("dfe-fetcher")
        for shape in fetchers:
            for transport in shapes.transports_for("both"):
                declared = shape.refusal(transport, (transport,), offered)
                assert declared == "does not deploy dfe-fetcher", f"{shape.name}: {declared}"

    def test_the_declared_substring_is_the_engine_refusal(self) -> None:
        # The live case asserts this substring against the engine's own 422, so a
        # reworded refusal has to fail here rather than on a deployment.
        fetcher = next(s for s in shapes.load_shapes() if s.origin == "fetcher")
        declared = str(fetcher.refusal("bus", ("bus",), self._without("dfe-fetcher")))

        with pytest.raises(FlowError, match=re.escape(declared)):
            resolve_flow(
                Source.model_validate({"source": "okta", "fetcher": {"source_type": "okta"}}),
                DFESettings(
                    env="dev",
                    transport={"default": "bus"},
                    deployment={"profile": "docker-slim"},
                ),
            )

    def test_a_deployment_that_deploys_one_is_untouched(self) -> None:
        for shape in shapes.load_shapes():
            for transport in shapes.transports_for("both"):
                if shape.skip_reason(transport):
                    continue
                assert shape.refusal(transport, (transport,), EVERY_APP) is None, shape.name

    def test_no_other_origin_reads_the_offer(self) -> None:
        # Only an origin with an app of its own is affected; the receiver is
        # stack-wide and every deployment runs one.
        for shape in shapes.load_shapes():
            if shape.origin == "fetcher":
                continue
            for transport in shapes.transports_for("both"):
                declared = shape.refusal(transport, (transport,), self._without("dfe-fetcher"))
                assert declared == shape.refusal(transport, (transport,), EVERY_APP), shape.name

    def test_the_transport_refusal_still_wins(self) -> None:
        # A transport the deployment does not carry refuses every shape, so it is
        # the reason reported even where the app is missing too.
        fetcher = next(s for s in shapes.load_shapes() if s.origin == "fetcher")
        declared = fetcher.refusal("direct", ("bus",), self._without("dfe-fetcher"))
        assert declared == "asks for the direct transport"


class TestTheSkipPolicy:
    def test_a_declared_skip_passes(self) -> None:
        assert (
            undeclared_skips([("a::b", f"{shapes.EXPECTED_SKIP} the archiver is bus only")]) == []
        )

    def test_anything_else_fails_the_run(self) -> None:
        skips = [("a::b", "clickhouse-connect is not installed")]
        assert undeclared_skips(skips) == skips


class TestTheNamesTheDeploymentGives:
    def test_the_catalogue_entry_fills_in_at_run_time(self) -> None:
        catalogue = next(s for s in shapes.load_shapes() if s.name == "catalogue")
        filled = catalogue.expect[0].substitute(entry="auth")
        assert filled.source == "auth"
        assert filled.table == "auth"
        assert filled.topic == "auth_land"
        assert filled.destination == "dfe-transform-elastic-auth"

    def test_the_catch_all_fills_in_from_whatever_the_release_calls_it(self) -> None:
        # The name is moving from `default` to `main`, so the fixture must carry
        # neither: the run reads routing.default_source off the deployment.
        default_flow = next(s for s in shapes.load_shapes() if s.name == "default-flow")

        for name in ("default", shapes.CATCHALL):
            filled = default_flow.expect[0].substitute(catchall=name)
            assert filled.source == name
            assert filled.table == name
            assert filled.topic == f"{name}_land"

    def test_no_fixture_hardcodes_a_catch_all_name(self) -> None:
        for shape in shapes.load_shapes():
            for expectation in shape.expect:
                assert expectation.table not in ("default", shapes.CATCHALL), (
                    f"{shape.name}: {expectation.table} is the catch-all's name, which "
                    "belongs to the deployment - write {catchall}"
                )
