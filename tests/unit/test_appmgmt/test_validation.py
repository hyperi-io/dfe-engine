#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_validation.py
#  Purpose:      Tests for syntax validation of authored transform files
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Validation gates a write, so a wrong answer here refuses valid work.

The Vector case is checked against the shape dfe-transform-vector actually
loads - a `transforms` map of named components - because a validator demanding
some other shape rejects every real file while looking like it works.
"""

import sys

import pytest

from dfe_engine.appmgmt import validation

# The shape dfe-transform-vector loads (src/config/transforms.rs).
REMAP = (
    "transforms:\n"
    "  parse:\n"
    "    type: remap\n"
    '    inputs: ["dfe_source"]\n'
    "    source: |\n"
    "      . = parse_json!(.message)\n"
)


class TestSwitch:
    def test_disabled_reports_disabled_rather_than_valid(self):
        result = validation.validate_language("yaml", "nonsense: [", enabled=False)
        assert result.status is validation.ValidationStatus.DISABLED
        assert result.blocks_write is False

    def test_a_language_with_no_backend_is_unavailable(self):
        result = validation.validate_language("cobol", "IDENTIFICATION", enabled=True)
        assert result.status is validation.ValidationStatus.UNAVAILABLE
        assert result.blocks_write is False

    def test_only_an_explicit_invalid_blocks_a_write(self):
        for status in validation.ValidationStatus:
            result = validation.ValidationResult(status=status)
            assert result.blocks_write is (status is validation.ValidationStatus.INVALID)


class TestVectorYaml:
    def test_the_shape_the_app_actually_loads_is_valid(self):
        result = validation.validate_language("yaml", REMAP, enabled=True)
        assert result.status is validation.ValidationStatus.VALID

    def test_several_components_in_one_file_are_valid(self):
        content = (
            "transforms:\n"
            "  parse:\n    type: remap\n    source: '.a = 1'\n"
            "  drop:\n    type: filter\n    condition: 'true'\n"
        )
        assert (
            validation.validate_language("yaml", content, enabled=True).status
            is validation.ValidationStatus.VALID
        )

    def test_a_bare_component_without_the_transforms_key_is_invalid(self):
        # Vector loads no components from this, so the deploy fails silently.
        content = "type: remap\nsource: '.a = 1'\n"
        result = validation.validate_language("yaml", content, enabled=True)
        assert result.status is validation.ValidationStatus.INVALID
        assert "transforms" in result.message

    def test_a_component_without_a_type_is_named_in_the_error(self):
        content = "transforms:\n  parse:\n    source: '.a = 1'\n"
        result = validation.validate_language("yaml", content, enabled=True)
        assert result.status is validation.ValidationStatus.INVALID
        assert "parse" in result.message

    def test_an_empty_transforms_map_is_invalid(self):
        result = validation.validate_language("yaml", "transforms: {}\n", enabled=True)
        assert result.status is validation.ValidationStatus.INVALID

    def test_invalid_yaml_is_invalid_and_carries_the_parse_error(self):
        result = validation.validate_language("yaml", "transforms: [unclosed\n", enabled=True)
        assert result.status is validation.ValidationStatus.INVALID
        assert result.errors

    def test_a_non_mapping_document_is_invalid(self):
        result = validation.validate_language("yaml", "- a\n- b\n", enabled=True)
        assert result.status is validation.ValidationStatus.INVALID


class TestVrl:
    def test_an_absent_backend_is_unavailable_never_valid(self, monkeypatch):
        # The soft seam: a missing checker must not read as a passing check.
        monkeypatch.setitem(sys.modules, "vectordotdev", None)
        result = validation.validate_language("vrl", ".a = 1", enabled=True)
        assert result.status is validation.ValidationStatus.UNAVAILABLE
        assert result.blocks_write is False

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            (True, validation.ValidationStatus.VALID),
            (False, validation.ValidationStatus.INVALID),
            ({"ok": True}, validation.ValidationStatus.VALID),
            ({"ok": False, "errors": ["boom"]}, validation.ValidationStatus.INVALID),
            ((True, []), validation.ValidationStatus.VALID),
            ((False, ["boom"]), validation.ValidationStatus.INVALID),
        ],
    )
    def test_the_backends_shapes_are_all_understood(self, monkeypatch, outcome, expected):
        # The binding is being reworked, so several return shapes are accepted.
        import types

        module = types.ModuleType("vectordotdev")
        module.vrl_check = lambda content: outcome  # ty: ignore[unresolved-attribute]  # a bare ModuleType takes any attribute
        monkeypatch.setitem(sys.modules, "vectordotdev", module)
        assert validation.validate_language("vrl", ".a = 1", enabled=True).status is expected

    def test_an_unrecognised_shape_is_unavailable_not_guessed(self, monkeypatch):
        import types

        module = types.ModuleType("vectordotdev")
        module.vrl_check = lambda content: 42  # ty: ignore[unresolved-attribute]  # a bare ModuleType takes any attribute
        monkeypatch.setitem(sys.modules, "vectordotdev", module)
        result = validation.validate_language("vrl", ".a = 1", enabled=True)
        assert result.status is validation.ValidationStatus.UNAVAILABLE

    def test_a_backend_that_raises_does_not_propagate(self, monkeypatch):
        import types

        def boom(content):
            raise RuntimeError("linked against nothing")

        module = types.ModuleType("vectordotdev")
        module.vrl_check = boom  # ty: ignore[unresolved-attribute]  # a bare ModuleType takes any attribute
        monkeypatch.setitem(sys.modules, "vectordotdev", module)
        result = validation.validate_language("vrl", ".a = 1", enabled=True)
        assert result.status is validation.ValidationStatus.UNAVAILABLE


class TestHostFunctions:
    """A VRL program may not call a function that reaches the pod environment or the network."""

    @pytest.mark.parametrize(
        ("program", "lines"),
        [
            ('.k = get_env_var!("KAFKA_SASL_PASSWORD")', [1]),
            ('x = 1\n.r = http_request("https://example.org")', [2]),
            ('.k = get_env_var ! ("X")\n.h = dns_lookup!(.host)', [1, 2]),
            ('.s = get_secret("kafka")', [1]),
        ],
        ids=["env", "http_on_line_two", "spaced_bang_and_two_calls", "secret"],
    )
    def test_a_call_is_refused_with_its_line(self, program, lines):
        result = validation.validate_language("vrl", program, enabled=False)

        assert result.status is validation.ValidationStatus.INVALID
        assert result.refused is True
        assert [int(e.split(":")[0].removeprefix("line ")) for e in result.errors] == lines

    @pytest.mark.parametrize(
        "program",
        [
            '.note = "call get_env_var(x) to read it"',
            '# get_env_var("X") is not allowed here\n.a = 1',
            ".a = my_get_env_var(.b)",
            ".a = parse_json!(.message)",
            "s = s'http_request(x)'",
        ],
        ids=["in_string", "in_comment", "longer_name", "ordinary_call", "in_raw_string"],
    )
    def test_a_name_that_is_not_a_call_passes(self, program):
        result = validation.validate_language("vrl", program, enabled=False)

        assert result.status is validation.ValidationStatus.DISABLED
        assert result.refused is False

    def test_the_operator_setting_lifts_the_refusal(self):
        result = validation.validate_language(
            "vrl", '.k = get_env_var!("X")', enabled=False, allow_host_functions=True
        )

        assert result.refused is False

    def test_yaml_is_not_scanned(self):
        result = validation.validate_language("yaml", "a: get_env_var(x)", enabled=False)

        assert result.refused is False
