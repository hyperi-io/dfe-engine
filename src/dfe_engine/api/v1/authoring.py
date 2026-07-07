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
models register under the same name (see dfe_engine.ai). RBAC: rule:read.
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

# Singular 'rule:' namespace (matches rule:* / rule:read in roles.yaml). A plural
# 'rules:read' matched NO role - permission_matches compares each colon segment
# literally, so 'rule:*' never covered 'rules:read' and only admin could author.
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
    query: str = Field(description="HyperDX query text to convert into a hunt rule")
    time_fields: list[str] | None = Field(
        default=None,
        description="Time-bound field names to strip (auto-detected when omitted)",
    )


@router.post("/from-hyperdx", dependencies=[_READ])
async def from_hyperdx(body: FromHyperdxRequest, user: CurrentUser) -> dict[str, Any]:
    """Strip a HyperDX query's time bounds -> {window}, ready to save as a rule."""
    return strip_hyperdx(body.query, body.time_fields)


class ScaffoldRequest(BaseModel):
    table: str = Field(description="Target table the SELECT reads from")
    columns: list[str] = Field(
        default_factory=list,
        description="Column names to project (defaults to discovered columns)",
    )
    source: str | None = Field(
        default=None, description="Source label to scope discovered columns to"
    )
    limit: int = Field(default=100, description="Row limit for the generated SELECT")


@router.post("/scaffold", dependencies=[_READ])
async def scaffold(body: ScaffoldRequest, user: CurrentUser) -> dict[str, str]:
    """A starter SELECT over discovered columns (source meta + landed _json keys)."""
    return {
        "sql": build_query_scaffold(body.columns, body.table, source=body.source, limit=body.limit)
    }


class ReviewRequest(BaseModel):
    sql: str = Field(description="SQL query to review")
    execution_profile: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional execution stats (row counts, timings) to inform the review",
    )


@router.post("/ai/review", dependencies=[_READ])
async def ai_review(body: ReviewRequest, user: CurrentUser, request: Request) -> AIModuleResult:
    """AI-review a SQL query for correctness and performance (QueryOptimiser)."""
    mod = _module(request, AIModuleType.QUERY_OPTIMISER)
    return mod.get_result(
        mod.submit(QueryOptimiser.Input(query=body.sql, execution_profile=body.execution_profile))
    )


class CreateRequest(BaseModel):
    prompt: str = Field(description="Natural-language description of the query to generate")
    source_name: str | None = Field(
        default=None, description="Source to target the generated query at"
    )
    columns: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Available columns (name/type dicts) to ground the generation",
    )


@router.post("/ai/create", dependencies=[_READ])
async def ai_create(body: CreateRequest, user: CurrentUser, request: Request) -> AIModuleResult:
    """Generate a SQL query from a natural-language prompt (QueryGenerator)."""
    mod = _module(request, AIModuleType.QUERY_GENERATOR)
    return mod.get_result(
        mod.submit(
            QueryGenerator.Input(
                prompt=body.prompt, source_name=body.source_name, columns=body.columns
            )
        )
    )


class VrlRequest(BaseModel):
    samples: list[str] = Field(description="Sample raw log lines to derive a VRL parser from")
    source_hint: str | None = Field(
        default=None, description="Optional source/format hint to guide parsing"
    )


@router.post("/ai/generate-vrl", dependencies=[_READ])
async def ai_generate_vrl(body: VrlRequest, user: CurrentUser, request: Request) -> AIModuleResult:
    """Generate a VRL parser from sample log lines (LogParser)."""
    mod = _module(request, AIModuleType.LOG_PARSER)
    return mod.get_result(
        mod.submit(LogParser.Input(samples=body.samples, source_hint=body.source_hint))
    )


class SchemaSuggestRequest(BaseModel):
    source_name: str = Field(description="Source whose schema to optimise")
    source_schema: dict[str, Any] = Field(
        default_factory=dict, description="Current source schema (column definitions)"
    )
    query_patterns: list[str] = Field(
        default_factory=list,
        description="Representative query patterns to optimise the schema for",
    )


@router.post("/ai/suggest-schema", dependencies=[_READ])
async def ai_suggest_schema(
    body: SchemaSuggestRequest, user: CurrentUser, request: Request
) -> AIModuleResult:
    """Suggest meta-schema column promotions from a source schema (SchemaOptimiser)."""
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
