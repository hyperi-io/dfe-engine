#  Project:      dfe-engine
#  File:         tests/unit/test_cel/test_parity.py
#  Purpose:      Python<->Rust CEL classifier parity test
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Python <-> Rust CEL classifier parity test.

Loads the shared fixture ``tests/fixtures/cel_classifier_parity.json`` and
runs every entry through the Python classifier in
``dfe_engine.cel.classify``. The Rust classifier in
``scalo-rs/src/transport/filter/classify.rs`` ships an identical
fixture and an identical test (``tests/transport_filter.rs``) that asserts
the same results.

If both tests pass on their respective sides, the Python UI validator and
the Rust runtime engine agree on tier classification, op extraction, and
field-reference extraction. If they diverge, the UI will validate filters
differently from the runtime -- this test catches that drift early.

The fixture file lives at ``tests/fixtures/cel_classifier_parity.json`` in both
repos. Keep the two byte-identical when adding cases.
"""

import json
from pathlib import Path

import pytest

from dfe_engine.cel.classify import FilterTier, classify_expression
from tests.support.producer_contract import SCALO_RS, producer_file

FIXTURE_PATH = Path(__file__).parents[2] / "fixtures" / "cel_classifier_parity.json"
_SCALO_RS_COPY = "tests/fixtures/cel_classifier_parity.json"


def _load_cases() -> list[dict]:
    with FIXTURE_PATH.open() as f:
        data = json.load(f)
    return data["cases"]


_TIER_NUMBERS = {
    FilterTier.TIER1: 1,
    FilterTier.TIER2: 2,
    FilterTier.TIER3: 3,
}


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["expression"])
def test_python_classifier_matches_fixture(case: dict) -> None:
    """Each fixture entry must match the Python classifier output exactly."""
    expr = case["expression"]
    expected_tier = case["tier"]

    result = classify_expression(expr)
    actual_tier = _TIER_NUMBERS[result.tier]

    assert actual_tier == expected_tier, (
        f"tier mismatch for {expr!r}: expected={expected_tier} actual={actual_tier}"
    )

    if expected_tier == 1:
        assert result.op is not None, f"Tier 1 result missing op for {expr!r}"
        assert result.op.kind == case["op_kind"], (
            f"op_kind mismatch for {expr!r}: expected={case['op_kind']} actual={result.op.kind}"
        )
        assert result.op.field == case["op_field"], (
            f"op_field mismatch for {expr!r}: expected={case['op_field']} actual={result.op.field}"
        )
        if "op_value" in case:
            assert result.op.value == case["op_value"], (
                f"op_value mismatch for {expr!r}: "
                f"expected={case['op_value']!r} actual={result.op.value!r}"
            )
    else:
        # Tier 2/3: compare sorted field lists (order is insertion-order
        # in both Python and Rust, but sorting makes the assertion robust
        # against future iteration changes).
        expected_fields = sorted(case.get("fields", []))
        actual_fields = sorted(result.fields or [])
        assert actual_fields == expected_fields, (
            f"fields mismatch for {expr!r}: expected={expected_fields} actual={actual_fields}"
        )


def test_fixture_is_in_sync_with_scalo_rs_copy() -> None:
    """The engine fixture must be byte-identical to the one scalo-rs ships.

    The scalo-rs copy comes from a checkout, else from the release pinned in
    ``tests/support/producer_contract.py``. Against a checkout, a mismatch is an
    edit on one side only: copy one file over the other. Against the pin, it is a
    case one repo has and the other has not released: sync the files, then move the
    pin to the scalo-rs release that carries them.
    """
    scalo_rs = producer_file(SCALO_RS, _SCALO_RS_COPY)
    assert FIXTURE_PATH.read_bytes() == scalo_rs.data, (
        f"{FIXTURE_PATH.name} differs from {scalo_rs.origin} - copy one over the other to re-sync"
    )
