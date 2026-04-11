#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_cel_endpoint.py
#  Purpose:      FastAPI TestClient tests for /v1/cel/check endpoints
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for the /v1/cel/check and /v1/cel/check-batch endpoints.

These endpoints power the live CEL validation in the TS UI. The router
wraps ``dfe_engine.cel.syntax.check_syntax`` which in turn classifies via
``dfe_engine.cel.classify`` and validates parser correctness via the
``cel-interpreter`` Rust crate (same engine rustlib uses at runtime).

Tests cover:
* Authentication required
* Single-expression validation (Tier 1, 2, 3)
* Batch validation
* Syntax errors surface in the response
* Tier classification matches the underlying classifier
* Field extraction is exposed in the response
"""

from __future__ import annotations

from fastapi.testclient import TestClient


class TestCelCheckEndpoint:
    def test_requires_auth(self, client: TestClient) -> None:
        resp = client.post("/api/v1/cel/check", json={"expression": "has(_table)"})
        assert resp.status_code == 401

    def test_tier1_has_field(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        resp = client.post(
            "/api/v1/cel/check",
            json={"expression": "has(_table)"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["errors"] == []
        assert data["tier"] == "tier1"
        assert data["tier_label"] == "Tier 1 (SIMD)"
        assert data["op_kind"] == "field_exists"
        assert data["op_field"] == "_table"

    def test_tier1_field_equals(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        resp = client.post(
            "/api/v1/cel/check",
            json={"expression": 'status == "poison"'},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["tier"] == "tier1"
        assert data["op_kind"] == "field_equals"
        assert data["op_field"] == "status"
        assert data["op_value"] == "poison"

    def test_tier2_compound_expression(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        resp = client.post(
            "/api/v1/cel/check",
            json={"expression": 'severity > 3 && source != "internal"'},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["tier"] == "tier2"
        assert data["tier_label"] == "Tier 2 (CEL)"
        assert sorted(data["fields"]) == ["severity", "source"]
        assert data["opt_in_required"] == "expression.allow_cel_filters_in (or _out)"

    def test_tier3_regex_classified_but_marked_opt_in(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        # Default check_profile=False — Tier 3 expressions are CLASSIFIED,
        # not REJECTED. The UI uses opt_in_required to warn the user.
        resp = client.post(
            "/api/v1/cel/check",
            json={"expression": 'host.matches("^prod-.*$")'},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["tier"] == "tier3"
        assert data["tier_label"] == "Tier 3 (complex CEL)"
        assert (
            data["opt_in_required"] == "expression.allow_complex_filters_in (or _out)"
        )

    def test_invalid_syntax_returns_errors(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        resp = client.post(
            "/api/v1/cel/check",
            json={"expression": "field == "},  # Trailing == with no rhs
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False
        assert len(data["errors"]) > 0
        # Tier is None when invalid
        assert data["tier"] is None

    def test_empty_expression_invalid(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        resp = client.post(
            "/api/v1/cel/check",
            json={"expression": ""},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False
        assert data["tier"] is None

    def test_check_profile_rejects_iteration(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        # When check_profile=True, the DFE profile rejects per-element
        # iteration functions (exists, all, map, filter, timestamp, duration).
        # Note: pylib's profile permits matches() — only iteration/time
        # functions are blocked at the profile level. The classifier still
        # marks matches() as Tier 3 for performance gating.
        resp = client.post(
            "/api/v1/cel/check",
            json={
                "expression": 'tags.exists(t, t == "pii")',
                "check_profile": True,
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False
        assert any(
            "exists" in e or "not allowed" in e.lower() for e in data["errors"]
        ), f"Expected profile error, got: {data['errors']}"


class TestCelCheckBatchEndpoint:
    def test_requires_auth(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/cel/check-batch",
            json={"expressions": ["has(_table)"]},
        )
        assert resp.status_code == 401

    def test_batch_classifies_each_expression(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        resp = client.post(
            "/api/v1/cel/check-batch",
            json={
                "expressions": [
                    "has(_table)",
                    'severity > 3',
                    'host.matches("^prod-.*$")',
                ]
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 3
        assert data["results"][0]["tier"] == "tier1"
        assert data["results"][1]["tier"] == "tier2"
        assert data["results"][2]["tier"] == "tier3"

    def test_batch_preserves_order(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        expressions = [
            "has(_a)",
            "has(_b)",
            "has(_c)",
        ]
        resp = client.post(
            "/api/v1/cel/check-batch",
            json={"expressions": expressions},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        for i, result in enumerate(data["results"]):
            assert result["op_field"] == f"_{chr(ord('a') + i)}"

    def test_batch_handles_mixed_validity(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        resp = client.post(
            "/api/v1/cel/check-batch",
            json={
                "expressions": [
                    "has(_table)",  # Valid Tier 1
                    "field == ",  # Invalid syntax
                    'severity > 3',  # Valid Tier 2
                ]
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 3
        assert data["results"][0]["valid"] is True
        assert data["results"][1]["valid"] is False
        assert data["results"][2]["valid"] is True

    def test_batch_max_length_enforced(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        # The router limits to 100 expressions per batch
        resp = client.post(
            "/api/v1/cel/check-batch",
            json={"expressions": ["has(_table)"] * 101},
            headers=admin_headers,
        )
        assert resp.status_code == 422  # Pydantic validation error
