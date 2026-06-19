#  Project:      dfe-engine
#  File:         src/dfe_engine/api/v1/cel.py
#  Purpose:      REST API for CEL expression validation + tier classification
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""CEL router — syntax check + tier classification for UI live validation.

Used by the TS UI to validate CEL expressions as the user types. Returns
structured results with:

- Real CEL parser errors (via ``cel-interpreter`` v0.10.0, same engine
  rustlib uses at runtime — zero drift between UI and runtime)
- Performance tier classification (Tier 1 / 2 / 3)
- Referenced fields, recognised operation, human-readable description
- Required config opt-ins for the tier

## Endpoints

- ``POST /cel/check`` — validate a single expression
- ``POST /cel/check-batch`` — validate a list of expressions in one call
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.cel import FilterTier
from dfe_engine.cel.syntax import SyntaxCheckResult, check_syntax

router = APIRouter(prefix="/cel", tags=["cel"])


# ── Request/Response models ─────────────────────────────────


class CelCheckRequest(BaseModel):
    """Request to validate a single CEL expression."""

    expression: str = Field(
        description="CEL expression to validate",
        examples=["has(_table)", 'severity > 3 && source != "internal"'],
    )
    check_profile: bool = Field(
        default=False,
        description=(
            "If true, apply DFE profile restrictions (reject regex/iteration/time). "
            "Leave false for transport filters — Tier 3 is classification only, "
            "not rejection."
        ),
    )


class CelCheckResponse(BaseModel):
    """Result of a single CEL expression check."""

    valid: bool = Field(description="True if syntax is valid")
    errors: list[str] = Field(
        default_factory=list,
        description="Syntax/profile errors (empty if valid)",
    )
    tier: FilterTier | None = Field(
        default=None,
        description="Performance tier (null if invalid)",
    )
    tier_label: str | None = Field(
        default=None,
        description="Human-readable tier label (e.g., 'Tier 1 (SIMD)')",
    )
    fields: list[str] = Field(
        default_factory=list,
        description="Fields referenced by the expression (Tier 2/3)",
    )
    op_kind: str | None = Field(
        default=None,
        description="Recognised Tier 1 operation kind (field_exists, field_equals, etc.)",
    )
    op_field: str | None = Field(
        default=None,
        description="Field name in the Tier 1 operation",
    )
    op_value: str | None = Field(
        default=None,
        description="Comparison value in the Tier 1 operation",
    )
    description: str | None = Field(
        default=None,
        description="Human-readable description of what the expression matches",
    )
    opt_in_required: str | None = Field(
        default=None,
        description="Config key that must be enabled for this expression's tier (Tier 2/3)",
    )

    @classmethod
    def from_result(cls, result: SyntaxCheckResult) -> CelCheckResponse:
        tier_label = None
        if result.tier == FilterTier.TIER1:
            tier_label = "Tier 1 (SIMD)"
        elif result.tier == FilterTier.TIER2:
            tier_label = "Tier 2 (CEL)"
        elif result.tier == FilterTier.TIER3:
            tier_label = "Tier 3 (complex CEL)"

        return cls(
            valid=result.valid,
            errors=result.errors,
            tier=result.tier,
            tier_label=tier_label,
            fields=result.fields,
            op_kind=result.op_kind,
            op_field=result.op_field,
            op_value=result.op_value,
            description=result.description,
            opt_in_required=result.opt_in_required,
        )


class CelCheckBatchRequest(BaseModel):
    """Request to validate multiple CEL expressions in one call."""

    expressions: list[str] = Field(
        description="List of CEL expressions to validate",
        max_length=100,
    )
    check_profile: bool = Field(default=False)


class CelCheckBatchResponse(BaseModel):
    """Result of a batch CEL expression check."""

    results: list[CelCheckResponse] = Field(
        description="One result per input expression, in the same order"
    )


# ── Endpoints ────────────────────────────────────────────────


@router.post(
    "/check",
    response_model=CelCheckResponse,
    summary="Validate a CEL expression and classify its performance tier",
    description=(
        "Returns syntax validity, performance tier, referenced fields, and a "
        "human-readable description of what the expression matches. "
        "Designed for live UI validation as the user types — completes in <1ms."
    ),
    dependencies=[Depends(require_action(scopes_dict["cel_check"]))],
)
async def check_cel_expression(
    request: CelCheckRequest,
    _user: CurrentUser,
) -> CelCheckResponse:
    """Validate a single CEL expression."""
    result = check_syntax(request.expression, check_profile=request.check_profile)
    return CelCheckResponse.from_result(result)


@router.post(
    "/check-batch",
    response_model=CelCheckBatchResponse,
    summary="Validate multiple CEL expressions in one call",
    description=(
        "Batch version of /cel/check. Use when the UI needs to validate an "
        "entire filter list at once (e.g., on form submit). Max 100 expressions."
    ),
    dependencies=[Depends(require_action(scopes_dict["cel_check"]))],
)
async def check_cel_expressions_batch(
    request: CelCheckBatchRequest,
    _user: CurrentUser,
) -> CelCheckBatchResponse:
    """Validate a list of CEL expressions."""
    results = [
        CelCheckResponse.from_result(check_syntax(expr, check_profile=request.check_profile))
        for expr in request.expressions
    ]
    return CelCheckBatchResponse(results=results)
