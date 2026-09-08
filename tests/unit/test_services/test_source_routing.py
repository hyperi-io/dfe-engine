"""Tests for source routing config generation.

Both emits target REAL Rust serde contracts - the receiver's
(dfe-receiver src/config/mod.rs SourceRule/RoutingConfig) and the loader's
(dfe-loader src/config/pipeline.rs RoutingConfig). The receiver side is pinned
by a round-trip over its exact field names; the loader side is pinned by
reading the Rust struct itself, because serde drops an unknown key without
complaint and a wrong key therefore has no signal until data lands in the wrong
table.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from dfe_engine.services.models.loader import LoaderConfig, LoaderRoutingConfig
from dfe_engine.services.models.receiver import (
    ReceiverRoutingConfig,
    SourceRule,
)
from dfe_engine.services.source_routing import (
    UnsupportedMatchOperatorError,
    compile_loader_routing,
    compile_receiver_routing,
)
from dfe_engine.source.models import Source
from dfe_engine.source.registry import SourceNotFoundError

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
    def __init__(self, sources: list[Source]) -> None:
        self._sources = {s.source: s for s in sources}

    def get_source(self, source_name: str) -> Source:
        if source_name not in self._sources:
            raise SourceNotFoundError(f"Source '{source_name}' not found")
        return self._sources[source_name]

    def get_all_sources(
        self, enabled_only: bool = False, *, states: tuple[str, ...] | None = None
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

_LOADER_DIR_ENV = "DFE_LOADER_DIR"
_PIPELINE_RS = Path("src") / "config" / "pipeline.rs"
_LOADER_RS = Path("src") / "config" / "loader.rs"

_UNPARSED = object()


@dataclass(frozen=True)
class RustStruct:
    """A Rust struct's field names and whatever of its defaults are literals."""

    fields: set[str]
    defaults: dict[str, object]


def _loader_source(relative: Path) -> Path | None:
    """A file inside dfe-loader, from the env override or a sibling checkout."""
    roots: list[Path] = []
    override = os.environ.get(_LOADER_DIR_ENV)
    if override:
        roots.append(Path(override))
    roots.append(Path(__file__).resolve().parents[3].parent / "dfe-loader")
    for root in roots:
        candidate = root / relative
        if candidate.is_file():
            return candidate
    return None


def _loader_pipeline_rs() -> Path | None:
    return _loader_source(_PIPELINE_RS)


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


def _parse_rust_fields(path: Path, name: str) -> set[str]:
    """A Rust struct's field names, for a struct that derives Default."""
    text = path.read_text(encoding="utf-8")
    struct_body = _strip_comments(_rust_block(text, f"pub struct {name} {{"))
    return set(re.findall(r"^\s*pub (\w+)\s*:", struct_body, re.MULTILINE))


def _parse_rust_struct(path: Path, name: str) -> RustStruct:
    text = path.read_text(encoding="utf-8")

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
    path = _loader_pipeline_rs()
    if path is None:
        pytest.skip(
            f"no dfe-loader checkout alongside this one - point {_LOADER_DIR_ENV} at "
            "one to check the engine model against the real loader contract"
        )
    parsed = _parse_rust_struct(path, "RoutingConfig")
    # A reshuffle upstream that defeats the parser must fail loudly here rather
    # than pass every assertion against an empty field set.
    assert "default_db" in parsed.fields, f"could not parse RoutingConfig out of {path}"
    return parsed


@pytest.fixture(scope="module")
def rust_loader_fields() -> set[str]:
    """dfe-loader's top-level Config field names, as the Rust source declares them."""
    path = _loader_source(_LOADER_RS)
    if path is None:
        pytest.skip(
            f"no dfe-loader checkout alongside this one - point {_LOADER_DIR_ENV} at "
            "one to check the engine model against the real loader contract"
        )
    fields = _parse_rust_fields(path, "Config")
    assert "clickhouse" in fields, f"could not parse Config out of {path}"
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


# ---------------------------------------------------------------------------
# Tests: SourceRule model (receiver serde contract)
# ---------------------------------------------------------------------------


class TestSourceRuleModel:
    def test_create(self):
        rule = SourceRule(
            field="agent.type", mode="key_value_set", match_value="filebeat", source="filebeat"
        )
        assert rule.field == "agent.type"
        assert rule.mode == "key_value_set"
        assert rule.match_value == "filebeat"
        assert rule.source == "filebeat"

    def test_serialization_matches_receiver_serde(self):
        rule = SourceRule(field="x", mode="key_value_set", match_value="y", source="z")
        d = rule.model_dump()
        # EXACT receiver field names (dfe-receiver src/config/mod.rs SourceRule)
        assert d == {"field": "x", "mode": "key_value_set", "match_value": "y", "source": "z"}

    def test_rejects_unknown_mode(self):
        with pytest.raises(ValueError):
            SourceRule(field="x", mode="regex_match", source="z")


class TestReceiverRoutingConfigModel:
    def test_defaults_match_receiver_defaults(self):
        config = ReceiverRoutingConfig()
        assert config.source_rules == []
        assert config.default_source == "default"
        assert config.topic_suffix == "_land"
        assert config.source_to_topic == {}
        assert config.legacy_compat is False


# ---------------------------------------------------------------------------
# Tests: compile_receiver_routing
# ---------------------------------------------------------------------------


class TestCompileReceiverRouting:
    def test_compiles_source_rules(self, registry):
        config = compile_receiver_routing(registry)
        assert config.default_source == "default"
        assert config.topic_suffix == "_land"

        # active sources with a match (disabled + dormant excluded)
        by_source = {r.source: r for r in config.source_rules}
        assert set(by_source) == {"filebeat", "syslog", "crowdstrike-edr", "present-src"}

    def test_equals_maps_to_key_value_set(self, registry):
        config = compile_receiver_routing(registry)
        fb = next(r for r in config.source_rules if r.source == "filebeat")
        assert fb.mode == "key_value_set"
        assert fb.field == "agent.type"
        assert fb.match_value == "filebeat"

    def test_exists_maps_to_key_present(self, registry):
        config = compile_receiver_routing(registry)
        pr = next(r for r in config.source_rules if r.source == "present-src")
        assert pr.mode == "key_present"
        assert pr.field == "tags.present_marker"
        assert pr.match_value is None

    def test_excludes_disabled_and_dormant_sources(self, registry):
        config = compile_receiver_routing(registry)
        stamped = {r.source for r in config.source_rules}
        assert "disabled-src" not in stamped
        assert "dormant-src" not in stamped

    def test_no_topic_overrides_for_default_derivation(self, registry):
        # topic_land == f"{source}{topic_suffix}" for every source, so no
        # source_to_topic entries are emitted.
        config = compile_receiver_routing(registry)
        assert config.source_to_topic == {}

    def test_unsupported_operator_skipped_with_warning(self):
        """A stored legacy-operator source is skipped LOUDLY, not a compile failure."""
        from scalo.logger import logger

        reg = FakeSourceRegistry(
            [_make_source("bad", match_field="f", match_value="v", match_operator="includes")]
        )
        captured: list[str] = []
        handler_id = logger.add(captured.append, level="WARNING")
        try:
            config = compile_receiver_routing(reg)
        finally:
            logger.remove(handler_id)

        assert config.source_rules == []  # the legacy source is absent, no raise
        assert any("documented receiver gap" in msg for msg in captured)
        assert any("'bad'" in msg and "'includes'" in msg for msg in captured)

    def test_unsupported_operator_does_not_brick_other_sources(self):
        """One legacy stored doc must not fail the whole receiver config compile."""
        reg = FakeSourceRegistry(
            [
                _make_source("bad", match_field="f", match_value="v", match_operator="includes"),
                _make_source("good", match_field="agent.type", match_value="good"),
                _make_source("present", match_field="tags.marker", match_operator="exists"),
            ]
        )
        config = compile_receiver_routing(reg)
        stamped = {r.source for r in config.source_rules}
        assert stamped == {"good", "present"}

    def test_unsupported_operator_error_message(self):
        """The registry save path still surfaces this error's message on save."""
        err = UnsupportedMatchOperatorError("bad", "includes")
        assert "documented receiver gap" in str(err)
        assert err.source == "bad"
        assert err.operator == "includes"

    def test_empty_registry(self):
        empty = FakeSourceRegistry([])
        config = compile_receiver_routing(empty)
        assert config.source_rules == []

    def test_round_trip_against_receiver_serde_shape(self, registry):
        """The emitted YAML/JSON round-trips against the receiver's serde shape.

        Pins the exact key set the Rust receiver deserialises:
        routing.source_rules[].{field,mode,match_value,source} +
        default_source + topic_suffix + source_to_topic.
        """
        from dfe_engine.yaml_utils import yaml_dump_string, yaml_load_string

        config = compile_receiver_routing(registry)
        emitted = yaml_load_string(yaml_dump_string(config.model_dump(mode="json")))

        assert set(emitted) == {
            "source_rules",
            "default_source",
            "topic_suffix",
            "source_to_topic",
            "legacy_compat",
            "dlq",
        }
        for rule in emitted["source_rules"]:
            assert set(rule) == {"field", "mode", "match_value", "source"}
            assert rule["mode"] in ("key_present", "key_value_set", "key_value_use")

        # the engine model itself re-validates the emitted doc (serde-compatible)
        assert ReceiverRoutingConfig.model_validate(emitted).default_source == "default"


# ---------------------------------------------------------------------------
# Tests: compile_loader_routing
# ---------------------------------------------------------------------------


class TestCompileLoaderRouting:
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
        # the loader had no field for.
        emitted = set(LoaderConfig().model_dump(mode="json", by_alias=True))
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
