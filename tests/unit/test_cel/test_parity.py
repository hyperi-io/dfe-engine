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
``hyperi-rustlib/src/transport/filter/classify.rs`` ships an identical
fixture and an identical test (``tests/transport_filter.rs``) that asserts
the same results.

If both tests pass on their respective sides, the Python UI validator and
the Rust runtime engine agree on tier classification, op extraction, and
field-reference extraction. If they diverge, the UI will validate filters
differently from the runtime — this test catches that drift early.

The fixture file lives in two places:

* ``/projects/dfe-engine/tests/fixtures/cel_classifier_parity.json`` (this side)
* ``/projects/hyperi-rustlib/tests/fixtures/cel_classifier_parity.json`` (Rust side)

Keep them byte-identical when adding cases.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dfe_engine.cel.classify import FilterTier, classify_expression

FIXTURE_PATH = Path(__file__).parents[2] / "fixtures" / "cel_classifier_parity.json"


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


RUSTLIB_COPY = Path("/projects/hyperi-rustlib/tests/fixtures/cel_classifier_parity.json")


def test_fixture_is_in_sync_with_rustlib_copy() -> None:
    """Sanity check: the dfe-engine fixture must be byte-identical to the
    rustlib copy. If this fails, run::

        cp tests/fixtures/cel_classifier_parity.json \\
           /projects/hyperi-rustlib/tests/fixtures/cel_classifier_parity.json
    """
    if not RUSTLIB_COPY.exists():
        pytest.skip("rustlib checkout not available; skipping cross-repo sync check")

    local_bytes = FIXTURE_PATH.read_bytes()
    rustlib_bytes = RUSTLIB_COPY.read_bytes()
    assert local_bytes == rustlib_bytes, (
        "Python and Rust fixtures have diverged — copy one over the other to re-sync"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The sync check keys on the absolute path /projects/hyperi-rustlib/..., which "
        "resolves only where a sibling checkout happens to sit, so it skips on every CI run "
        "and the two fixtures can diverge unreported. The Rust copy must be reachable from "
        "repo state -- a submodule, or a vendored copy under tests/fixtures/ refreshed by a "
        "release step. Remove this marker once it is."
    ),
)
def test_rustlib_fixture_reference_resolves_from_repo_state() -> None:
    """The cross-repo sync check must not depend on a machine-specific path.

    A guard whose precondition is "someone happens to have cloned another repo
    next door" is a guard CI never runs. The reference has to come from
    something the repo carries.
    """
    assert not RUSTLIB_COPY.is_absolute() or RUSTLIB_COPY.is_relative_to(
        Path(__file__).resolve().parents[3]
    ), f"cross-repo fixture reference is outside the repo: {RUSTLIB_COPY}"
