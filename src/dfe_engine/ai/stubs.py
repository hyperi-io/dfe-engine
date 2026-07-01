#  Project:      dfe-engine
#  File:         ai/stubs.py
#  Purpose:      Default no-op stub implementations of the AI module types
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Default STUB implementations of the AI module types (interface.py).

The framework defines the contracts; real models live in separate repos and register
via ``AIModuleRegistry``. Until migrated, these stubs make the four touch-points
functional - each returns a COMPLETED result marked ``stub: True``, echoing enough of
the input that the UI/CLI can round-trip. Migrating a real model = registering a
concrete implementation (Derek's code) under the same name.

Touch-points: review a query (QueryOptimiser), create a query from a prompt
(QueryGenerator), generate VRL from samples / ClickHouse ``_json`` (LogParser), and
suggest meta-schema column promotions (SchemaOptimiser).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .interface import (
    AIModuleRegistry,
    AIModuleResult,
    AIModuleStatus,
    LogParser,
    QueryGenerator,
    QueryOptimiser,
    SchemaOptimiser,
)


class _StubExec:
    """Mixin: a synchronous stub whose submit() computes a COMPLETED marked result."""

    def __init__(self) -> None:
        self._results: dict[str, AIModuleResult] = {}

    def submit(self, input_data: BaseModel) -> str:
        task_id = self.generate_task_id()  # type: ignore[attr-defined]
        self._results[task_id] = AIModuleResult(
            task_id=task_id,
            module_name=self.name,  # type: ignore[attr-defined]
            module_type=self.module_type,  # type: ignore[attr-defined]
            status=AIModuleStatus.COMPLETED,
            output={"stub": True, **self._stub_output(input_data)},
        )
        return task_id

    def get_result(self, task_id: str) -> AIModuleResult:
        if task_id not in self._results:
            raise KeyError(task_id)
        return self._results[task_id]

    def _stub_output(self, input_data: BaseModel) -> dict[str, Any]:
        return {"note": "AI module not migrated yet - register a real implementation"}


class StubQueryOptimiser(_StubExec, QueryOptimiser):
    """ai:review - echoes the query unchanged. Migrate a real optimiser."""

    @property
    def name(self) -> str:
        return "stub-query-optimiser"

    def _stub_output(self, input_data: BaseModel) -> dict[str, Any]:
        return {
            "proposed_query": getattr(input_data, "query", ""),
            "rationale": "stub - not migrated",
        }


class StubQueryGenerator(_StubExec, QueryGenerator):
    """ai:create - returns an empty query echoing the prompt. Migrate a real generator."""

    @property
    def name(self) -> str:
        return "stub-query-generator"

    def _stub_output(self, input_data: BaseModel) -> dict[str, Any]:
        return {
            "proposed_query": "",
            "rationale": f"stub for prompt: {getattr(input_data, 'prompt', '')}",
        }


class StubLogParser(_StubExec, LogParser):
    """VRL generation - returns an empty parser. Migrate the real (CH _json) parser."""

    @property
    def name(self) -> str:
        return "stub-log-parser"

    def _stub_output(self, input_data: BaseModel) -> dict[str, Any]:
        return {
            "parser_config": {},
            "proposed_schema": {},
            "sample_count": len(getattr(input_data, "samples", []) or []),
            "confidence": 0.0,
        }


class StubSchemaOptimiser(_StubExec, SchemaOptimiser):
    """Meta-schema column promotion - returns no proposals. Migrate a real optimiser."""

    @property
    def name(self) -> str:
        return "stub-schema-optimiser"

    def _stub_output(self, input_data: BaseModel) -> dict[str, Any]:
        return {"proposed_columns": [], "rationale": "stub - not migrated"}


def default_ai_registry() -> AIModuleRegistry:
    """A registry pre-loaded with the four stub modules (functional defaults)."""
    registry = AIModuleRegistry()
    registry.register(StubQueryOptimiser())
    registry.register(StubQueryGenerator())
    registry.register(StubLogParser())
    registry.register(StubSchemaOptimiser())
    return registry
