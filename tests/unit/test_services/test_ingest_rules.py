#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_ingest_rules.py
#  Purpose:      Compound Source conditions, the rule order and compatibility profiles compile
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The receiver's rule set as the sources, the order and the bound profiles compile it.

Each compiled set is also evaluated with scalo.rules under the CEL Fast profile the
receiver compiles with, so a golden that looks right but routes wrong fails here,
and its rule block is walked against dfe-receiver's own config schema at the
pinned release. Records carry Elastic's ``data_stream.dataset``, the identifier a
source routes on.
"""

import json
from typing import Any

import pytest
from dfe_schemas.compat import load_compat_profile
from scalo.logger import logger
from scalo.rules import CompileOptions
from scalo.rules import RuleSet as ScaloRuleSet

from dfe_engine.ingest.models import CompatProfile, IngestRules, RuleOrder
from dfe_engine.services.source_routing import (
    MAX_SOURCE_ROWS,
    RECEIVER_RULES_PROFILE,
    CompiledRules,
    MatchTooWideError,
    compile_ingest_rules,
    match_rows,
)
from dfe_engine.source.models import CompatBinding, Source, SourceMatch
from tests.support.config_schema import definitions, type_mismatches, unread_keys
from tests.support.producer_contract import DFE_RECEIVER, producer_file

LEGACY = "dfe-legacy-2x"
ELASTIC = "elastic-agent"


def _source(name: str, **body: Any) -> Source:
    return Source.model_validate({"source": name, **body})


def _dataset(value: str, **extra: Any) -> dict[str, Any]:
    return {"field": "data_stream.dataset", "value": value, **extra}


def _shipped(profile_id: str) -> CompatProfile:
    return CompatProfile.model_validate(load_compat_profile(profile_id))


def _ingest(
    *defaults: CompatBinding,
    order: RuleOrder | None = None,
    profiles: tuple[CompatProfile, ...] | None = None,
) -> IngestRules:
    held = profiles if profiles is not None else (_shipped(LEGACY), _shipped(ELASTIC))
    return IngestRules(
        order=order or RuleOrder(),
        profiles={profile.id: profile for profile in held},
        defaults=defaults,
    )


def _compile(*sources: Source, ingest: IngestRules | None = None) -> CompiledRules:
    return compile_ingest_rules(sources, ingest or IngestRules())


def _dumped(compiled: CompiledRules) -> dict[str, Any]:
    return compiled.rule_set.model_dump(mode="json", exclude_defaults=True)


def _rows(compiled: CompiledRules) -> list[dict[str, Any]]:
    return _dumped(compiled)["rules"]


def _route(compiled: CompiledRules, record: dict[str, Any]) -> dict[str, str | None]:
    """The ``_source`` and ``_org_id`` scalo.rules assigns *record*, as the receiver evaluates it."""
    rules = ScaloRuleSet.compile(_dumped(compiled), CompileOptions(profile=RECEIVER_RULES_PROFILE))
    outcome = rules.evaluate(record)
    return {target: outcome.value(target) for target in ("_source", "_org_id")}


def _ids(compiled: CompiledRules, target: str) -> list[str]:
    return [block.id for block in compiled.blocks[target]]


def _row(rule_id: str, match: dict[str, Any], label: str) -> dict[str, Any]:
    return {"id": rule_id, "match": match, "set": {"_source": {"value": label}}}


def _test(value: str, **extra: Any) -> dict[str, Any]:
    return {"field": "data_stream.dataset", "op": "equals", "value": value, **extra}


# ---------------------------------------------------------------------------
# Compound conditions
# ---------------------------------------------------------------------------


class TestCompoundMatch:
    def test_all_compiles_to_one_row(self):
        source = _source(
            "cisco-ios",
            match={
                "all": [_dataset("cisco_ios.log"), {"field": "agent.type", "value": "filebeat"}]
            },
        )
        assert _rows(_compile(source)) == [
            _row(
                "source.cisco-ios",
                {
                    "all": [
                        _test("cisco_ios.log"),
                        {"field": "agent.type", "op": "equals", "value": "filebeat"},
                    ]
                },
                "cisco-ios",
            )
        ]

    def test_each_any_branch_is_a_row_in_declared_order(self):
        source = _source(
            "cisco-meraki",
            match={"any": [_dataset("cisco_meraki.log"), _dataset("cisco_meraki.events")]},
        )
        assert _rows(_compile(source)) == [
            _row("source.cisco-meraki.1", _test("cisco_meraki.log"), "cisco-meraki"),
            _row("source.cisco-meraki.2", _test("cisco_meraki.events"), "cisco-meraki"),
        ]

    @pytest.mark.parametrize(("operator", "inverted"), [("equals", True), ("not_equals", False)])
    def test_a_negated_test_inverts_the_receiver_test(self, operator, inverted):
        source = _source(
            "x", match={"field": "agent.type", "operator": operator, "value": "a", "negate": True}
        )
        [row] = _rows(_compile(source))
        assert row["match"].get("negate", False) is inverted

    def test_a_negated_all_stays_one_row(self):
        source = _source("x", match={"negate": True, "all": [_dataset("a.log"), _dataset("b.log")]})
        assert _rows(_compile(source)) == [
            _row("source.x", {"all": [_test("a.log"), _test("b.log")], "negate": True}, "x")
        ]

    def test_a_negated_any_is_one_row_of_negated_tests(self):
        source = _source("x", match={"negate": True, "any": [_dataset("a.log"), _dataset("b.log")]})
        assert _rows(_compile(source)) == [
            _row(
                "source.x",
                {"all": [_test("a.log", negate=True), _test("b.log", negate=True)]},
                "x",
            )
        ]

    def test_an_any_inside_an_all_repeats_the_rest_per_branch(self):
        source = _source(
            "x",
            match={
                "all": [
                    {"field": "agent.type", "value": "filebeat"},
                    {"any": [_dataset("a.log"), _dataset("b.log")]},
                ]
            },
        )
        agent = {"field": "agent.type", "op": "equals", "value": "filebeat"}
        assert _rows(_compile(source)) == [
            _row("source.x.1", {"all": [agent, _test("a.log")]}, "x"),
            _row("source.x.2", {"all": [agent, _test("b.log")]}, "x"),
        ]

    def test_a_negated_all_holding_an_any_follows_de_morgan(self):
        # not (a and (b or c)) is (not a) or (not b and not c).
        source = _source(
            "x",
            match={
                "negate": True,
                "all": [_dataset("a.log"), {"any": [_dataset("b.log"), _dataset("c.log")]}],
            },
        )
        assert [row["match"] for row in _rows(_compile(source))] == [
            _test("a.log", negate=True),
            {"all": [_test("b.log", negate=True), _test("c.log", negate=True)]},
        ]

    def test_when_is_one_row_carrying_the_condition(self):
        when = "data_stream.dataset == 'okta.system' && agent.type == 'filebeat'"
        assert _rows(_compile(_source("okta", when=when))) == [
            {"id": "source.okta", "when": when, "set": {"_source": {"value": "okta"}}}
        ]

    def test_a_match_past_the_row_cap_is_refused(self):
        pairs = [{"any": [_dataset(f"{n}.a"), _dataset(f"{n}.b")]} for n in range(6)]
        with pytest.raises(MatchTooWideError, match=str(MAX_SOURCE_ROWS)):
            match_rows(SourceMatch.model_validate({"all": pairs}))

    def test_the_rows_route_the_records_they_describe(self):
        compiled = _compile(
            _source(
                "cisco-ios",
                match={
                    "all": [_dataset("cisco_ios.log"), {"field": "agent.type", "value": "filebeat"}]
                },
            ),
            _source(
                "cisco-meraki",
                match={"any": [_dataset("cisco_meraki.log"), _dataset("cisco_meraki.events")]},
            ),
            _source(
                "umbrella",
                match={
                    "all": [
                        {
                            "field": "data_stream.dataset",
                            "operator": "starts_with",
                            "value": "cisco_umbrella.",
                        },
                        {"field": "agent.type", "value": "winlogbeat", "negate": True},
                    ]
                },
            ),
            _source(
                "okta", when="data_stream.dataset == 'okta.system' && agent.type == 'filebeat'"
            ),
        )

        def routed(dataset: str, agent: str) -> str | None:
            record = {"data_stream": {"dataset": dataset}, "agent": {"type": agent}}
            return _route(compiled, record)["_source"]

        assert routed("cisco_ios.log", "filebeat") == "cisco-ios"
        assert routed("cisco_ios.log", "winlogbeat") is None
        assert routed("cisco_meraki.log", "filebeat") == "cisco-meraki"
        assert routed("cisco_meraki.events", "winlogbeat") == "cisco-meraki"
        assert routed("cisco_umbrella.dns", "filebeat") == "umbrella"
        assert routed("cisco_umbrella.dns", "winlogbeat") is None
        assert routed("okta.system", "filebeat") == "okta"
        assert routed("okta.system", "winlogbeat") is None

    def test_the_rule_block_is_one_the_receiver_reads(self):
        schema = json.loads(producer_file(DFE_RECEIVER, "docs/config-schema.json").text)
        compiled = _compile(
            _source("a", match={"negate": True, "any": [_dataset("a.log"), _dataset("b.log")]}),
            _source("b", when="data_stream.dataset == 'b.log'"),
            _source(
                "c",
                match=_dataset("c.log"),
                aliases={LEGACY: ["auth"]},
                compat_profiles=[{"profile": LEGACY}],
            ),
            ingest=_ingest(),
        )
        config = {"rules": _dumped(compiled)}
        defs = definitions(schema)
        assert unread_keys(config, schema, defs) == []
        assert type_mismatches(config, schema, defs) == []


# ---------------------------------------------------------------------------
# Compatibility profiles
# ---------------------------------------------------------------------------


class TestCompatProfiles:
    def _legacy_pair(self) -> tuple[Source, Source]:
        return (
            _source("syslog", match=_dataset("system.syslog"), aliases={LEGACY: ["syslog-2x"]}),
            _source(
                "auth",
                match=_dataset("system.auth"),
                aliases={LEGACY: ["auth-2x", "authentication"]},
            ),
        )

    def test_a_bound_profile_expands_to_rows_held_to_its_sources(self):
        compiled = _compile(*self._legacy_pair(), ingest=_ingest(CompatBinding(profile=LEGACY)))

        def org(row: str, field: str, label: str) -> dict[str, Any]:
            placeholders = [
                {"field": field, "op": "exists"},
                {"field": field, "op": "equals", "value": "unknown", "negate": True},
                {"field": field, "op": "equals", "value": "dfesystem", "negate": True},
            ]
            return {
                "id": f"compat.{LEGACY}.{row}.{label}",
                "match": {
                    "all": [
                        {"field": "_source", "op": "equals", "value": label},
                        {"all": placeholders},
                    ]
                },
                "set": {"_org_id": {"from": field}},
            }

        assert _dumped(compiled) == {
            "targets": ["_source", "_org_id"],
            "lookups": {
                f"compat.{LEGACY}.aliases": {
                    "auth-2x": "auth",
                    "authentication": "auth",
                    "syslog-2x": "syslog",
                }
            },
            "rules": [
                _row("source.auth", _test("system.auth"), "auth"),
                _row("source.syslog", _test("system.syslog"), "syslog"),
                {
                    "id": f"compat.{LEGACY}.category",
                    "set": {
                        "_source": {
                            "from": "tags.event.category",
                            "lookup": f"compat.{LEGACY}.aliases",
                        }
                    },
                },
                org("org-tags", "tags.event.org_id", "auth"),
                org("org-tags", "tags.event.org_id", "syslog"),
                org("org-flat", "org_id", "auth"),
                org("org-flat", "org_id", "syslog"),
            ],
        }
        assert _ids(compiled, "_source") == [
            "source.auth",
            "source.syslog",
            f"compat.{LEGACY}",
            "default",
        ]
        assert _ids(compiled, "_org_id") == [f"compat.{LEGACY}", "default"]

    def test_the_profile_rows_route_a_2x_record_to_its_source_and_org(self):
        compiled = _compile(*self._legacy_pair(), ingest=_ingest(CompatBinding(profile=LEGACY)))

        def routed(category: str, org_id: str) -> dict[str, str | None]:
            return _route(compiled, {"tags": {"event": {"category": category, "org_id": org_id}}})

        assert routed("authentication", "acme") == {"_source": "auth", "_org_id": "acme"}
        assert routed("syslog-2x", "acme") == {"_source": "syslog", "_org_id": "acme"}
        # A 2.1 placeholder is no tenant, and an unaliased category is no source.
        assert routed("authentication", "unknown") == {"_source": "auth", "_org_id": None}
        assert routed("firewall", "acme") == {"_source": None, "_org_id": None}

    def test_an_opt_in_row_compiles_only_for_the_source_that_opts_in(self):
        compiled = _compile(
            _source(
                "cisco-ios",
                match={"field": "event.module", "value": "cisco_ios"},
                aliases={ELASTIC: ["cisco_ios.log"]},
                compat_profiles=[{"profile": ELASTIC, "opt_in": ["org-namespace"]}],
            ),
            _source(
                "cisco-asa",
                match={"field": "event.module", "value": "cisco_asa"},
                aliases={ELASTIC: ["cisco_asa.log"]},
                compat_profiles=[{"profile": ELASTIC}],
            ),
            ingest=_ingest(),
        )
        org_rows = [row["id"] for row in _rows(compiled) if "_org_id" in row["set"]]
        assert org_rows == [f"compat.{ELASTIC}.org-namespace.cisco-ios"]

        def routed(dataset: str) -> dict[str, str | None]:
            return _route(compiled, {"data_stream": {"dataset": dataset, "namespace": "acme"}})

        assert routed("cisco_ios.log") == {"_source": "cisco-ios", "_org_id": "acme"}
        assert routed("cisco_asa.log") == {"_source": "cisco-asa", "_org_id": None}

    def test_an_empty_list_binds_no_profile_whatever_the_deployment_binds(self):
        compiled = _compile(
            _source("auth", match=_dataset("system.auth"), compat_profiles=[]),
            ingest=_ingest(CompatBinding(profile=LEGACY)),
        )
        assert [row["id"] for row in _rows(compiled)] == ["source.auth"]

    def test_no_bound_profile_compiles_no_lookup(self):
        compiled = _compile(_source("auth", match=_dataset("system.auth")), ingest=_ingest())
        assert "lookups" not in _dumped(compiled)

    def test_two_versions_of_one_profile_skip_it_loudly(self):
        doc = load_compat_profile(LEGACY)
        doc["versions"]["1.1.0"] = {**doc["versions"]["1.0.0"], "type": "addition"}
        both = CompatProfile.model_validate(doc)
        captured: list[str] = []
        handler = logger.add(captured.append, level="WARNING")
        try:
            compiled = _compile(
                _source(
                    "a",
                    match=_dataset("a.log"),
                    compat_profiles=[{"profile": LEGACY, "version": "1.0.0"}],
                ),
                _source(
                    "b",
                    match=_dataset("b.log"),
                    compat_profiles=[{"profile": LEGACY, "version": "1.1.0"}],
                ),
                ingest=_ingest(profiles=(both,)),
            )
        finally:
            logger.remove(handler)
        assert [row["id"] for row in _rows(compiled)] == ["source.a", "source.b"]
        assert any("one version per deployment" in message for message in captured)

    def test_a_block_the_receiver_refuses_is_left_out_and_still_listed(self):
        doc = load_compat_profile(LEGACY)
        doc["id"] = "regex-org"
        doc["resource_type"] = "custom"
        doc["versions"]["1.0.0"]["rules"] = [
            {"id": "org", "when": 'org_id.matches("^a")', "set": {"_org_id": {"from": "org_id"}}}
        ]
        doc["versions"]["1.0.0"]["lookups"] = {}
        refused = CompatProfile.model_validate(doc)
        compiled = _compile(
            _source("a", match=_dataset("a.log"), compat_profiles=[{"profile": "regex-org"}]),
            ingest=_ingest(profiles=(refused,)),
        )
        assert [row["id"] for row in _rows(compiled)] == ["source.a"]
        assert compiled.dropped == ("compat.regex-org",)
        assert _ids(compiled, "_org_id") == ["compat.regex-org", "default"]
        assert any("matches()" in warning for warning in compiled.warnings["compat.regex-org"])


# ---------------------------------------------------------------------------
# The rule order
# ---------------------------------------------------------------------------


class TestRuleOrder:
    def _three(self) -> tuple[Source, ...]:
        return (
            _source("a", match=_dataset("a.log")),
            _source("b", match=_dataset("b.log")),
            _source(
                "c",
                match=_dataset("c.log"),
                aliases={LEGACY: ["c-2x"]},
                compat_profiles=[{"profile": LEGACY}],
            ),
        )

    def test_with_no_order_sources_run_by_name_then_profiles(self):
        compiled = _compile(*self._three(), ingest=_ingest())
        assert _ids(compiled, "_source") == [
            "source.a",
            "source.b",
            "source.c",
            f"compat.{LEGACY}",
            "default",
        ]

    def test_the_stored_order_places_the_blocks(self):
        order = RuleOrder.model_validate(
            {"_source": [f"compat.{LEGACY}", "source.c", "source.b", "source.a"]}
        )
        compiled = _compile(*self._three(), ingest=_ingest(order=order))
        assert _ids(compiled, "_source") == [
            f"compat.{LEGACY}",
            "source.c",
            "source.b",
            "source.a",
            "default",
        ]
        assert [row["id"] for row in _rows(compiled)][:3] == [
            f"compat.{LEGACY}.category",
            "source.c",
            "source.b",
        ]

    def test_a_block_the_order_leaves_out_runs_after_the_ones_it_names(self):
        order = RuleOrder.model_validate({"_source": ["source.b", "source.gone"]})
        compiled = _compile(*self._three(), ingest=_ingest(order=order))
        assert _ids(compiled, "_source") == [
            "source.b",
            "source.a",
            "source.c",
            f"compat.{LEGACY}",
            "default",
        ]

    def test_first_match_follows_the_order(self):
        both = _dataset("shared.log")
        first = _source("first", match={"all": [both]})
        second = _source("second", match={"any": [both]})
        record = {"data_stream": {"dataset": "shared.log"}}
        by_name = _compile(first, second)
        reordered = _compile(
            first,
            second,
            ingest=IngestRules(order=RuleOrder.model_validate({"_source": ["source.second"]})),
        )
        assert _route(by_name, record)["_source"] == "first"
        assert _route(reordered, record)["_source"] == "second"


# ---------------------------------------------------------------------------
# Shadow warnings
# ---------------------------------------------------------------------------


class TestShadowWarnings:
    def test_an_earlier_exists_on_the_field_shadows_a_later_test_on_it(self):
        compiled = _compile(
            _source("a", match={"field": "agent.type", "operator": "exists"}),
            _source("b", match={"field": "agent.type", "value": "filebeat"}),
        )
        [warning] = compiled.warnings["source.b"]
        assert warning.startswith('source.b: never fires: row "source.a"')
        assert "source.a" not in compiled.warnings

    def test_an_identical_condition_later_is_shadowed(self):
        order = RuleOrder.model_validate({"_source": ["source.b", "source.a"]})
        compiled = _compile(
            _source("a", match={"all": [_dataset("x.log")]}),
            _source("b", match=_dataset("x.log")),
            ingest=IngestRules(order=order),
        )
        [warning] = compiled.warnings["source.a"]
        assert warning.startswith('source.a: never fires: row "source.b"')

    def test_a_narrower_test_first_shadows_nothing(self):
        compiled = _compile(
            _source("a", match={"field": "agent.type", "value": "filebeat"}),
            _source("b", match={"field": "agent.type", "operator": "exists"}),
        )
        assert compiled.warnings == {}

    def test_a_lookup_row_shadows_nothing(self):
        # A lookup misses on an unknown key, so the rows after it still fire.
        order = RuleOrder.model_validate({"_source": [f"compat.{LEGACY}", "source.b"]})
        compiled = _compile(
            _source(
                "a",
                match=_dataset("a.log"),
                aliases={LEGACY: ["a-2x"]},
                compat_profiles=[{"profile": LEGACY}],
            ),
            _source("b", match={"field": "tags.event.category", "value": "b"}),
            ingest=_ingest(order=order),
        )
        assert "source.b" not in compiled.warnings
