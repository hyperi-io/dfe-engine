#  Project:      dfe-engine
#  File:         tests/unit/test_gitops/test_pins.py
#  Purpose:      Tests for the deploy repo's pins.yaml reader
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The pins.yaml reader over real files on disk (no mocks).

Every accessor has to answer for a deploy repo that has no pins.yaml, one that
is half-filled in, and one an operator has broken - the two surfaces reading it
(the startup notice and the console footer) both have a safe answer without it.
"""

from __future__ import annotations

from dfe_engine.gitops.pins import component_overrides, load_pins, stack_version

FULL = (
    "base:\n"
    '  dfe-infra: "2.2.0"\n'
    '  dfe-schemas: "2.2.0"\n'
    'channel: "release"\n'
    "overrides:\n"
    "  apps:\n"
    '    dfe-receiver: "v1.16.0@sha256:abc"\n'
    '    dfe-loader: "v1.19.0@sha256:def"\n'
)


def _write_pins(tmp_path, text: str) -> None:
    (tmp_path / "pins.yaml").write_text(text)


def test_no_pins_file_loads_empty(tmp_path):
    assert load_pins(tmp_path) == {}


def test_malformed_pins_never_raises(tmp_path):
    _write_pins(tmp_path, "{{{ not yaml")
    assert load_pins(tmp_path) == {}


def test_non_mapping_pins_loads_empty(tmp_path):
    _write_pins(tmp_path, "- a\n- b\n")
    assert load_pins(tmp_path) == {}


def test_stack_version_is_the_dfe_infra_base_pin(tmp_path):
    _write_pins(tmp_path, FULL)
    assert stack_version(load_pins(tmp_path)) == "2.2.0"


def test_stack_version_is_none_without_a_base_block(tmp_path):
    _write_pins(tmp_path, 'channel: "release"\n')
    assert stack_version(load_pins(tmp_path)) is None


def test_stack_version_is_none_when_pins_are_absent(tmp_path):
    assert stack_version(load_pins(tmp_path)) is None


def test_overrides_flattened_across_groups(tmp_path):
    _write_pins(tmp_path, FULL)
    assert component_overrides(load_pins(tmp_path)) == {
        "dfe-receiver": "v1.16.0@sha256:abc",
        "dfe-loader": "v1.19.0@sha256:def",
    }


def test_pins_without_overrides_is_empty(tmp_path):
    _write_pins(tmp_path, 'base:\n  dfe-infra: "2.2.0"\n')
    assert component_overrides(load_pins(tmp_path)) == {}
