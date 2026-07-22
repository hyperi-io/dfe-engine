#  Project:      dfe-engine
#  File:         api/v1/authoring.py
#  Purpose:      Rule/transform authoring helpers - HyperDX->rule, scaffold, AI assist
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Authoring helpers (compute-only - saving a rule is the rules CRUD).

POST /authoring/from-hyperdx     -> strip HyperDX time bounds to the hunt {window}
POST /authoring/scaffold         -> a starter SELECT over discovered columns
POST /authoring/ai/review        -> AI query review (QueryOptimiser)
POST /authoring/ai/create        -> AI query from a prompt (QueryGenerator)
POST /authoring/ai/generate-vrl  -> AI VRL from samples (LogParser)
POST /authoring/ai/suggest-schema-> AI meta-schema promotions (SchemaOptimiser)

The AI endpoints submit to the AI module registry - stub modules by default; real
models register under the same name (see dfe_engine.ai). RBAC: rules:read.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dfe_engine.ai import (
    AIModuleResult,
    AIModuleType,
    LogParser,
    QueryGenerator,
    QueryOptimiser,
    SchemaOptimiser,
    default_ai_registry,
)
from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.rule_authoring import build_query_scaffold, strip_hyperdx

router = APIRouter(prefix="/authoring", tags=["Rules: Authoring"])

_READ = Depends(require_action(scopes_dict["rule_read"]))


def _registry(request: Request):
    return getattr(request.app.state, "ai_registry", None) or default_ai_registry()


def _module(request: Request, module_type: AIModuleType):
    modules = _registry(request).list_modules(module_type)
    if not modules:
        raise HTTPException(
            503,
            detail={"code": "no_ai_module", "message": f"no {module_type.value} module registered"},
        )
    return modules[0]


class FromHyperdxRequest(BaseModel):
    query: str
    time_fields: list[str] | None = None


@router.post("/from-hyperdx", dependencies=[_READ])
async def from_hyperdx(body: FromHyperdxRequest, user: CurrentUser) -> dict[str, Any]:
    """Strip a HyperDX query's time bounds -> {window}, ready to save as a rule."""
    return strip_hyperdx(body.query, body.time_fields)


class ScaffoldRequest(BaseModel):
    table: str
    columns: list[str] = Field(default_factory=list)
    source: str | None = None
    limit: int = 100


@router.post("/scaffold", dependencies=[_READ])
async def scaffold(body: ScaffoldRequest, user: CurrentUser) -> dict[str, str]:
    """A starter SELECT over discovered columns (source meta + landed _json keys)."""
    return {
        "sql": build_query_scaffold(body.columns, body.table, source=body.source, limit=body.limit)
    }


class ReviewRequest(BaseModel):
    sql: str
    execution_profile: dict[str, Any] = Field(default_factory=dict)


@router.post("/ai/review", dependencies=[_READ])
async def ai_review(body: ReviewRequest, user: CurrentUser, request: Request) -> AIModuleResult:
    mod = _module(request, AIModuleType.QUERY_OPTIMISER)
    return mod.get_result(
        mod.submit(QueryOptimiser.Input(query=body.sql, execution_profile=body.execution_profile))
    )


class CreateRequest(BaseModel):
    prompt: str
    source_name: str | None = None
    columns: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/ai/create", dependencies=[_READ])
async def ai_create(body: CreateRequest, user: CurrentUser, request: Request) -> AIModuleResult:
    mod = _module(request, AIModuleType.QUERY_GENERATOR)
    return mod.get_result(
        mod.submit(
            QueryGenerator.Input(
                prompt=body.prompt, source_name=body.source_name, columns=body.columns
            )
        )
    )


class VrlRequest(BaseModel):
    samples: list[str]
    source_hint: str | None = None


@router.post("/ai/generate-vrl", dependencies=[_READ])
async def ai_generate_vrl(body: VrlRequest, user: CurrentUser, request: Request) -> AIModuleResult:
    mod = _module(request, AIModuleType.LOG_PARSER)
    return mod.get_result(
        mod.submit(LogParser.Input(samples=body.samples, source_hint=body.source_hint))
    )


class SchemaSuggestRequest(BaseModel):
    source_name: str
    source_schema: dict[str, Any] = Field(default_factory=dict)
    query_patterns: list[str] = Field(default_factory=list)


@router.post("/ai/suggest-schema", dependencies=[_READ])
async def ai_suggest_schema(
    body: SchemaSuggestRequest, user: CurrentUser, request: Request
) -> AIModuleResult:
    mod = _module(request, AIModuleType.SCHEMA_OPTIMISER)
    return mod.get_result(
        mod.submit(
            SchemaOptimiser.Input(
                source_name=body.source_name,
                source_schema=body.source_schema,
                query_patterns=body.query_patterns,
            )
        )
    )
