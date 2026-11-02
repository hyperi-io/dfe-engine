"""Tests for source routing config generation.

Both emits target REAL Rust serde contracts - the receiver's
(dfe-receiver src/config/mod.rs RoutingConfig and scalo-rs rules::RuleSetConfig)
and the loader's (dfe-loader src/config/pipeline.rs RoutingConfig). The receiver
side is pinned by goldens over its exact field names. The loader side is pinned
by reading the Rust struct itself, because serde drops an unknown key without
complaint and a wrong key therefore has no signal until data lands in the wrong
table. The struct comes from a dfe-loader checkout, else from the release pinned
in ``tests/support/producer_contract.py``.
"""

import re
from collections.abc import Collection
from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from dfe_engine.ingest.models import IngestRules
from dfe_engine.services.models.loader import LoaderConfig, LoaderRoutingConfig
from dfe_engine.services.models.receiver import (
    DestinationsConfig,
    IngestBindingsConfig,
    ReceiverRoutingConfig,
    Rule,
    RuleSet,
)
from dfe_engine.services.registry import stored_form
from dfe_engine.services.source_routing import (
    MAX_SOURCE_ROWS,
    compile_loader_routing,
    compile_receiver_routing,
    compile_receiver_rules,
)
from dfe_engine.source.models import Source
from dfe_engine.source.registry import SourceNotFoundError
from tests.support.producer_contract import DFE_LOADER, ProducerFile, producer_file

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source(
    name: str,
    *,
    match_field: str | None = None,
    match_value: str | None = None,
    match_operator: str = "equals",
    state: str = "active",
) -> Source:
    data: dict = {"source": name, "state": state}
    if match_field:
        data["match"] = {
            "field": match_field,
            "operator": match_operator,
            "value": match_value or "",
        }
    return Source.model_validate(data)


class FakeSourceRegistry:
    def __init__(self, sources: list[Source], ingest: IngestRules | None = None) -> None:
        self._sources = {s.source: s for s in sources}
        self._ingest = ingest or IngestRules()

    def ingest_rules(self) -> IngestRules:
        return self._ingest

    def get_source(self, source_name: str) -> Source:
        if source_name not in self._sources:
            raise SourceNotFoundError(f"Source '{source_name}' not found")
        return self._sources[source_name]

    def get_all_sources(
        self, enabled_only: bool = False, *, states: Collection[str] | None = None
    ) -> list[Source]:
        sources = list(self._sources.values())
        if states is not None:
            sources = [s for s in sources if s.state in states]
        elif enabled_only:
            sources = [s for s in sources if s.enabled]
        return sources


# ---------------------------------------------------------------------------
# The dfe-loader serde contract, read out of the Rust that defines it
# ---------------------------------------------------------------------------

_PIPELINE_RS = "src/config/pipeline.rs"
_LOADER_RS = "src/config/loader.rs"

_UNPARSED = object()


@dataclass(frozen=True)
class RustStruct:
    """A Rust struct's field names and whatever of its defaults are literals."""

    fields: set[str]
    defaults: dict[str, object]


def _rust_block(text: str, header: str) -> str:
    """The brace-balanced body that follows ``header``."""
    start = text.index(header) + len(header)
    depth = 1
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i]
    raise AssertionError(f"unbalanced braces after {header!r}")


def _strip_comments(body: str) -> str:
    return re.sub(r"//[^\n]*", "", body)


def _split_top_level(body: str) -> list[str]:
    """Comma-separated items at nesting depth zero, strings kept intact."""
    parts: list[str] = []
    current = ""
    depth = 0
    in_string = False
    escaped = False
    for ch in body:
        if in_string:
            current += ch
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            current += ch
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return [stripped for stripped in (part.strip() for part in parts) if stripped]


def _unwrap(expr: str, prefix: str, suffix: str) -> str | None:
    if expr.startswith(prefix) and expr.endswith(suffix):
        return expr[len(prefix) : len(expr) - len(suffix)]
    return None


def _rust_literal(expr: str) -> object:
    """A Rust default expression as its Python equivalent, or ``_UNPARSED``.

    Only the forms the config structs actually use are handled - anything else
    (a nested ``::default()``, a computed value) comes back unparsed and is
    left out of the comparison rather than guessed at.
    """
    expr = " ".join(expr.split())
    if expr == "None":
        return None
    if expr in ("true", "false"):
        return expr == "true"
    if expr.endswith("HashMap::new()"):
        return {}
    inner = _unwrap(expr, "Some(", ")")
    if inner is not None:
        return _rust_literal(inner)
    inner = _unwrap(expr, "vec![", "]")
    if inner is not None:
        return [_rust_literal(item) for item in _split_top_level(inner)]
    match = re.fullmatch(r'"((?:[^"\\]|\\.)*)"(?:\.to_string\(\)|\.into\(\))?', expr)
    if match:
        return match.group(1)
    return _UNPARSED


def _parse_rust_fields(source: ProducerFile, name: str) -> set[str]:
    """A Rust struct's field names, for a struct that derives Default."""
    struct_body = _strip_comments(_rust_block(source.text, f"pub struct {name} {{"))
    return set(re.findall(r"^\s*pub (\w+)\s*:", struct_body, re.MULTILINE))


def _parse_rust_struct(source: ProducerFile, name: str) -> RustStruct:
    text = source.text

    struct_body = _strip_comments(_rust_block(text, f"pub struct {name} {{"))
    fields = set(re.findall(r"^\s*pub (\w+)\s*:", struct_body, re.MULTILINE))

    impl_body = _rust_block(text, f"impl Default for {name} {{")
    fn_body = _rust_block(impl_body, "fn default() -> Self {")
    literal_body = _strip_comments(_rust_block(fn_body, "Self {"))

    defaults: dict[str, object] = {}
    for assignment in _split_top_level(literal_body):
        field, sep, expr = assignment.partition(":")
        if not sep:
            continue
        value = _rust_literal(expr)
        if value is not _UNPARSED:
            defaults[field.strip()] = value

    return RustStruct(fields=fields, defaults=defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rust_routing() -> RustStruct:
    """dfe-loader's RoutingConfig, as the Rust source declares it."""
    source = producer_file(DFE_LOADER, _PIPELINE_RS)
    parsed = _parse_rust_struct(source, "RoutingConfig")
    # A reshuffle upstream that defeats the parser must fail loudly here rather
    # than pass every assertion against an empty field set.
    assert "default_db" in parsed.fields, f"could not parse RoutingConfig out of {source.origin}"
    return parsed


@pytest.fixture(scope="module")
def rust_loader_fields() -> set[str]:
    """dfe-loader's top-level Config field names, as the Rust source declares them."""
    source = producer_file(DFE_LOADER, _LOADER_RS)
    fields = _parse_rust_fields(source, "Config")
    assert "clickhouse" in fields, f"could not parse Config out of {source.origin}"
    return fields


@pytest.fixture
def sources():
    return [
        _make_source("filebeat", match_field="agent.type", match_value="filebeat"),
        _make_source("syslog", match_field="tags.event.category", match_value="syslog"),
        _make_source(
            "crowdstrike-edr", match_field="_source_fetcher", match_value="crowdstrike"
        ),  # SaaS fetcher source — still carries a match
        _make_source("present-src", match_field="tags.present_marker", match_operator="exists"),
        _make_source("disabled-src", match_field="x", match_value="y", state="disabled"),
        _make_source("dormant-src", match_field="d", match_value="d", state="dormant"),
    ]


@pytest.fixture
def registry(sources):
    return FakeSourceRegistry(sources)


def _fetched_source(name: str, *, topic: str = "own", state: str = "active") -> Source:
    return Source.model_validate(
        {
            "source": name,
            "state": state,
            "fetcher": {"source_type": "okta", "topic": topic, "config": {}},
        }
    )


def _rules(registry) -> list[dict]:
    """The compiled ``_source`` rows, as the receiver's overlay carries them."""
    return compile_receiver_rules(registry).model_dump(mode="json", exclude_defaults=True)["rules"]


class TestFetcherSourcesInReceiverRules:
    """The fetcher stamps ``_source`` with the landing label, and a row keeps it."""

    def test_an_own_topic_source_gets_an_explicit_row(self):
        assert _rules(FakeSourceRegistry([_fetched_source("okta-audit")])) == [
            {
                "id": "source.okta-audit",
                "match": {"field": "_source", "op": "equals", "value": "okta-audit"},
                "set": {"_source": {"value": "okta-audit"}},
            }
        ]

    def test_a_main_topic_source_needs_no_row(self):
        # default_source already sends an unmatched record to the main topic.
        rules = compile_receiver_rules(
            FakeSourceRegistry([_fetched_source("okta-audit", topic="main")])
        )
        assert rules.rules == []

    @pytest.mark.parametrize("state", ["dormant", "disabled"])
    def test_a_source_that_is_not_active_gets_no_row(self, state):
        rules = compile_receiver_rules(
            FakeSourceRegistry([_fetched_source("okta-audit", state=state)])
        )
        assert rules.rules == []

    def test_the_loader_maps_a_fetched_source_to_its_table(self):
        config = compile_loader_routing(FakeSourceRegistry([_fetched_source("okta-audit")]))
        assert config.source_to_table == {"okta-audit": "okta-audit"}


# ---------------------------------------------------------------------------
# Tests: the receiver models (dfe-receiver and scalo-rs serde contracts)
# ---------------------------------------------------------------------------


class TestRuleModel:
    def test_a_row_serialises_with_the_scalo_field_names(self):
        rule = Rule.model_validate(
            {
                "id": "copy",
                "match": {"field": "a", "op": "exists"},
                "set": {"_org_id": {"from": "b"}},
            }
        )
        assert rule.model_dump(mode="json", exclude_defaults=True) == {
            "id": "copy",
            "match": {"field": "a", "op": "exists"},
            "set": {"_org_id": {"from": "b"}},
        }

    def test_a_row_sets_exactly_one_field(self):
        with pytest.raises(ValidationError, match="exactly one field"):
            Rule.model_validate(
                {"id": "two", "set": {"_source": {"value": "a"}, "_org_id": {"value": "b"}}}
            )

    def test_a_row_sets_only_a_receiver_target(self):
        with pytest.raises(ValidationError, match="_source, _org_id"):
            Rule.model_validate({"id": "x", "set": {"host": {"value": "a"}}})

    def test_a_misspelt_key_is_refused_as_the_receiver_refuses_it(self):
        with pytest.raises(ValidationError):
            Rule.model_validate({"id": "x", "set": {"_source": {"vaule": "a"}}})

    def test_the_receiver_s_target_order_is_the_only_one_taken(self):
        with pytest.raises(ValidationError, match="in that order"):
            RuleSet(targets=["_org_id", "_source"])


class TestReceiverRoutingConfigModel:
    def test_defaults_match_receiver_defaults(self):
        config = ReceiverRoutingConfig()
        assert config.default_source == "main"
        assert config.default_org_id == "default"
        assert config.source_org_id == "disallow"
        assert config.topic_suffix == "_land"
        assert config.source_to_topic == {}

    @pytest.mark.parametrize("retired", ["source_rules", "legacy_compat"])
    def test_a_retired_routing_key_is_refused(self, retired):
        # The receiver refuses an unknown routing key, so the engine must not write one.
        with pytest.raises(ValidationError, match=retired):
            ReceiverRoutingConfig.model_validate({retired: []})

    def test_an_empty_default_org_is_refused(self):
        with pytest.raises(ValidationError):
            ReceiverRoutingConfig(default_org_id="")


class TestIngestBindingsModel:
    def test_a_default_outside_the_binding_is_refused(self):
        # The receiver refuses the whole config for it, so every listener stops.
        with pytest.raises(ValidationError, match="not one of orgs"):
            IngestBindingsConfig.model_validate(
                {"credentials": {"agents": {"orgs": ["acme"], "default_org": "beta"}}}
            )

    def test_a_binding_with_no_org_is_refused(self):
        with pytest.raises(ValidationError):
            IngestBindingsConfig.model_validate({"credentials": {"agents": {"orgs": []}}})


class TestDestinationsModel:
    """The receiver's ``DestinationRef`` is one name or a list (src/config/mod.rs)."""

    ARCHIVER = {"grpc": {"endpoint": "http://dfe-archiver:6000"}}

    def _fanned(self, **extra):
        return DestinationsConfig(
            default="loader", sources={"auth": ["loader", "dfe-archiver"]}, **extra
        )

    def test_a_source_may_name_several_destinations(self):
        config = self._fanned(**{"dfe-archiver": self.ARCHIVER})

        assert config.sources["auth"] == ["loader", "dfe-archiver"]

    def test_a_fan_out_serialises_as_the_list_the_receiver_reads(self):
        dumped = self._fanned(**{"dfe-archiver": self.ARCHIVER}).model_dump(mode="json")

        assert dumped["sources"]["auth"] == ["loader", "dfe-archiver"]

    def test_an_undeclared_name_inside_a_fan_out_is_still_refused(self):
        # Every name in the list is resolved, so one that is neither built in nor
        # declared drops the copy it was meant to take.
        with pytest.raises(ValueError, match="dfe-archiver"):
            self._fanned()

    def test_the_default_may_fan_out_too(self):
        config = DestinationsConfig.model_validate(
            {"default": ["loader", "dfe-archiver"], "dfe-archiver": self.ARCHIVER}
        )

        assert config.default == ["loader", "dfe-archiver"]

    @pytest.mark.parametrize(
        ("config", "key"),
        [
            pytest.param({"default": []}, "destinations.default", id="default"),
            pytest.param({"sources": {"auth": []}}, "destinations.sources.auth", id="source"),
        ],
    )
    def test_a_fan_out_naming_nothing_is_refused_by_its_key(self, config, key):
        # The receiver answers such a record delivered while sending it nowhere.
        with pytest.raises(ValueError, match=rf"{key} is an empty list"):
            DestinationsConfig.model_validate(config)

    @pytest.mark.parametrize(
        "config",
        [{"default": ["kafka"]}, {"sources": {"auth": ["kafka"]}}],
        ids=["default", "source"],
    )
    def test_the_same_lists_naming_a_destination_validate(self, config):
        DestinationsConfig.model_validate(config)

    def test_the_retired_rules_list_is_refused_as_a_named_destination(self):
        with pytest.raises(ValueError, match="rules"):
            DestinationsConfig.model_validate({"rules": []})


# ---------------------------------------------------------------------------
# Tests: compile_receiver_rules and compile_receiver_routing
# ---------------------------------------------------------------------------


class TestCompileReceiverRules:
    def test_the_golden_rule_set(self, registry):
        """Every active matched source, by name while no order names them, as the receiver reads it."""
        assert compile_receiver_rules(registry).model_dump(mode="json", exclude_defaults=True) == {
            "targets": ["_source", "_org_id"],
            "rules": [
                {
                    "id": "source.crowdstrike-edr",
                    "match": {"field": "_source_fetcher", "op": "equals", "value": "crowdstrike"},
                    "set": {"_source": {"value": "crowdstrike-edr"}},
                },
                {
                    "id": "source.filebeat",
                    "match": {"field": "agent.type", "op": "equals", "value": "filebeat"},
                    "set": {"_source": {"value": "filebeat"}},
                },
                {
                    "id": "source.present-src",
                    "match": {"field": "tags.present_marker", "op": "exists"},
                    "set": {"_source": {"value": "present-src"}},
                },
                {
                    "id": "source.syslog",
                    "match": {"field": "tags.event.category", "op": "equals", "value": "syslog"},
                    "set": {"_source": {"value": "syslog"}},
                },
            ],
        }

    def test_no_row_sets_an_org(self, registry):
        # A record's org is its sender's binding, else routing.default_org_id.
        targets = {next(iter(rule.set)) for rule in compile_receiver_rules(registry).rules}
        assert targets == {"_source"}

    @pytest.mark.parametrize(
        ("operator", "test"),
        [
            ("includes", {"field": "f", "op": "contains", "value": "v"}),
            ("starts_with", {"field": "f", "op": "starts_with", "value": "v"}),
            ("ends_with", {"field": "f", "op": "ends_with", "value": "v"}),
            ("not_equals", {"field": "f", "op": "equals", "value": "v", "negate": True}),
        ],
    )
    def test_every_operator_compiles_to_a_receiver_test(self, operator, test):
        reg = FakeSourceRegistry(
            [_make_source("one", match_field="f", match_value="v", match_operator=operator)]
        )
        assert _rules(reg) == [
            {"id": "source.one", "match": test, "set": {"_source": {"value": "one"}}}
        ]

    def test_a_match_too_wide_is_skipped_loudly_and_bricks_no_other_source(self):
        """One stored doc must not fail the whole receiver config compile."""
        from scalo.logger import logger

        branches = [{"field": "f", "value": str(n)} for n in range(MAX_SOURCE_ROWS + 1)]
        wide = Source.model_validate({"source": "wide", "match": {"any": branches}})
        reg = FakeSourceRegistry(
            [wide, _make_source("good", match_field="agent.type", match_value="good")]
        )
        captured: list[str] = []
        handler_id = logger.add(captured.append, level="WARNING")
        try:
            rules = compile_receiver_rules(reg)
        finally:
            logger.remove(handler_id)

        assert [rule.id for rule in rules.rules] == ["source.good"]
        assert any("'wide'" in msg and str(MAX_SOURCE_ROWS) in msg for msg in captured)

    def test_empty_registry(self):
        assert compile_receiver_rules(FakeSourceRegistry([])).rules == []

    def test_the_emitted_set_round_trips_through_the_model(self, registry):
        from dfe_engine.yaml_utils import yaml_dump_string, yaml_load_string

        emitted = compile_receiver_rules(registry).model_dump(mode="json", exclude_defaults=True)
        reread = RuleSet.model_validate(yaml_load_string(yaml_dump_string(emitted)))
        assert reread == compile_receiver_rules(registry)


class TestCompileReceiverRouting:
    def test_the_golden_routing_block(self, registry):
        config = compile_receiver_routing(registry)
        assert config.model_dump(mode="json", exclude_unset=True) == {
            "default_source": "main",
            "default_org_id": "default",
            "topic_suffix": "_land",
            "source_to_topic": {},
            "source_org_id": "disallow",
        }

    def test_the_deployment_may_keep_a_sender_s_org(self, registry):
        config = compile_receiver_routing(registry, source_org_id="allow")
        assert config.source_org_id == "allow"

    def test_the_emitted_block_carries_no_retired_key(self, registry):
        emitted = compile_receiver_routing(registry).model_dump(mode="json")
        assert not {"source_rules", "legacy_compat"} & set(emitted)


# ---------------------------------------------------------------------------
# Tests: compile_loader_routing
# ---------------------------------------------------------------------------


class TestCompileLoaderRouting:
    def test_the_golden_routing_block(self, registry):
        """What reaches the loader's overlay: the database, the default org and the table map."""
        compiled = compile_loader_routing(registry, db="prod").model_dump(
            mode="json", exclude_unset=True
        )
        assert compiled == {
            "default_db": "prod",
            "default_org_id": "default",
            "source_to_table": {
                "filebeat": "filebeat",
                "syslog": "syslog",
                "crowdstrike-edr": "crowdstrike-edr",
                "present-src": "present-src",
            },
        }

    def test_routes_on_the_field_the_receiver_stamps(self, registry):
        # _source is what compile_receiver_routing stamps, and the loader's own
        # default table_fields, so the two halves meet with no map at all.
        config = compile_loader_routing(registry)
        assert config.table_fields == ["_source"]
        assert config.default_db == "dfe"
        assert config.default_table == "main"

    def test_source_to_table_populated(self, registry):
        config = compile_loader_routing(registry)
        # All ACTIVE sources should appear
        assert "filebeat" in config.source_to_table
        assert "syslog" in config.source_to_table
        assert "crowdstrike-edr" in config.source_to_table
        # Disabled + dormant excluded
        assert "disabled-src" not in config.source_to_table
        assert "dormant-src" not in config.source_to_table

    def test_custom_db(self, registry):
        config = compile_loader_routing(registry, db="prod")
        assert config.default_db == "prod"

    def test_empty_registry(self):
        empty = FakeSourceRegistry([])
        config = compile_loader_routing(empty)
        assert config.source_to_table == {}
        assert config.table_fields == ["_source"]


# ---------------------------------------------------------------------------
# Tests: LoaderRoutingConfig against the dfe-loader serde contract
# ---------------------------------------------------------------------------


class TestLoaderRoutingAgainstRustContract:
    """The engine model pinned to dfe-loader's RoutingConfig, field by field.

    serde drops an unknown key in silence, so a key the engine invents does not
    fail the loader - it never takes effect and the loader silently uses its own
    default. That failure has no signal anywhere until data lands in the wrong
    table, which is why the two schemas are compared here rather than trusted to
    stay in step.
    """

    def test_every_emitted_key_exists_in_the_rust_struct(self, rust_routing):
        emitted = set(LoaderRoutingConfig().model_dump(mode="json"))
        unknown = emitted - rust_routing.fields
        assert not unknown, (
            f"the loader's RoutingConfig has no {sorted(unknown)} - serde would drop "
            f"{'them' if len(unknown) > 1 else 'it'} without a word and fall back to "
            "its own default"
        )

    def test_the_shared_defaults_agree(self, rust_routing):
        ours = LoaderRoutingConfig().model_dump(mode="json")
        shared = {k: v for k, v in rust_routing.defaults.items() if k in ours}
        # Guards the assertion below against a parse that quietly found nothing.
        assert len(shared) >= 6, f"only parsed {sorted(shared)} out of the Rust defaults"
        for name, expected in shared.items():
            assert ours[name] == expected, (
                f"routing.{name} defaults to {ours[name]!r} here and {expected!r} in "
                "the loader - the loader's value is the one that ships"
            )

    def test_every_top_level_key_exists_in_the_rust_config(self, rust_loader_fields):
        # Scoped to RoutingConfig alone, this pin missed an entire emitted block
        # the loader had no field for. The keys are the ones stored_form writes,
        # since a by_alias dump spells them as no file on disk does.
        emitted = set(stored_form(LoaderConfig()))
        unknown = emitted - rust_loader_fields
        assert not unknown, (
            f"the loader's Config has no {sorted(unknown)} - serde would drop "
            f"{'them' if len(unknown) > 1 else 'it'} without a word, so the knob "
            "would read as live while doing nothing"
        )

    def test_the_compiled_block_is_serde_compatible(self, registry, rust_routing):
        # What actually reaches a deployed loader is the compiled block, so the
        # compile output is checked and not only the bare model.
        compiled = compile_loader_routing(registry, db="prod").model_dump(mode="json")
        assert set(compiled) <= rust_routing.fields
