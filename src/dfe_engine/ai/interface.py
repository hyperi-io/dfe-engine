"""AI Module Interface -- ABC contracts for pluggable AI modules.

Engine defines the interface contracts. Implementations live in separate
repos and register via ``AIModuleRegistry``. Three module types:

1. **QueryOptimiser** -- takes a query + execution profile, returns a
   proposed optimised query with rationale.
2. **SchemaOptimiser** -- takes a source schema, returns proposed
   meta-schema improvements.
3. **LogParser** -- takes raw log samples, returns parsers + proposed
   meta-schema.

All modules execute asynchronously. The engine submits work via
``submit()`` and retrieves results via ``get_result()``. The actual
execution may be in-process, a background thread, or a remote TS runner.

Usage::

    from dfe_engine.ai import AIModuleRegistry, QueryOptimiser

    registry = AIModuleRegistry()
    registry.register(MyQueryOptimiser())

    module = registry.get("my-query-opt")
    task_id = module.submit(QueryOptimiser.Input(
        query="SELECT ...",
        execution_profile={...},
    ))
    result = module.get_result(task_id)
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field
from scalo.logger import logger

# -- Enums --------------------------------------------------


class AIModuleType(str, Enum):
    """Discriminator for AI module types."""

    QUERY_OPTIMISER = "query_optimiser"
    QUERY_GENERATOR = "query_generator"
    SCHEMA_OPTIMISER = "schema_optimiser"
    LOG_PARSER = "log_parser"


class AIModuleStatus(str, Enum):
    """Status of an async AI module task."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# -- Result model -------------------------------------------


class AIModuleResult(BaseModel):
    """Result of an AI module execution."""

    task_id: str = Field(..., description="Unique task identifier")
    module_name: str = Field(..., description="Module that produced this result")
    module_type: AIModuleType = Field(..., description="Module type")
    status: AIModuleStatus = Field(default=AIModuleStatus.PENDING, description="Task status")
    output: dict[str, Any] = Field(default_factory=dict, description="Module-specific output")
    error: str | None = Field(default=None, description="Error message if failed")
    duration_ms: float | None = Field(
        default=None, description="Execution duration in milliseconds"
    )


# -- Base ABC -----------------------------------------------


class AIModuleInterface(ABC):
    """Abstract base class for all AI modules.

    Subclasses must implement:
    - ``name`` -- unique module identifier
    - ``module_type`` -- one of AIModuleType
    - ``submit()`` -- accept input and return a task_id
    - ``get_result()`` -- retrieve result by task_id
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique module identifier (e.g. 'gpt4-query-opt')."""

    @property
    @abstractmethod
    def module_type(self) -> AIModuleType:
        """Module type discriminator."""

    @abstractmethod
    def submit(self, input_data: BaseModel) -> str:
        """Submit work to the module. Returns a task_id for async retrieval.

        Args:
            input_data: Module-specific Pydantic input model.

        Returns:
            task_id string for use with ``get_result()``.
        """

    @abstractmethod
    def get_result(self, task_id: str) -> AIModuleResult:
        """Retrieve the result of a submitted task.

        Args:
            task_id: ID returned by ``submit()``.

        Returns:
            AIModuleResult with status and output.

        Raises:
            KeyError: If task_id is unknown.
        """

    def generate_task_id(self) -> str:
        """Generate a unique task ID. Modules may override."""
        return f"{self.name}:{uuid.uuid4().hex[:12]}"


# -- Typed module ABCs --------------------------------------


class QueryOptimiser(AIModuleInterface):
    """Takes a query + execution profile, returns proposed optimised query."""

    class Input(BaseModel):
        """Input for query optimisation."""

        query: str = Field(..., description="SQL query to optimise")
        execution_profile: dict[str, Any] = Field(
            default_factory=dict,
            description="EXPLAIN output, row counts, timing data",
        )
        source_table: str | None = Field(default=None, description="Source table name")
        context: dict[str, Any] = Field(default_factory=dict, description="Additional context")

    class Output(BaseModel):
        """Output from query optimisation."""

        proposed_query: str = Field(..., description="Optimised SQL query")
        rationale: str = Field(default="", description="Explanation of changes")
        estimated_improvement: str | None = Field(
            default=None,
            description="Estimated improvement (e.g. '3x fewer rows scanned')",
        )

    @property
    def module_type(self) -> AIModuleType:
        return AIModuleType.QUERY_OPTIMISER


class QueryGenerator(AIModuleInterface):
    """Takes a natural-language prompt + context, returns a proposed query."""

    class Input(BaseModel):
        """Input for query generation."""

        prompt: str = Field(..., description="Natural-language description of the query")
        source_name: str | None = Field(default=None, description="Source to query")
        columns: list[dict[str, Any]] = Field(
            default_factory=list,
            description="Available columns (from source meta + landed _json)",
        )
        context: dict[str, Any] = Field(default_factory=dict, description="Additional context")

    class Output(BaseModel):
        """Output from query generation."""

        proposed_query: str = Field(..., description="Generated SQL query")
        rationale: str = Field(default="", description="How the prompt maps to the query")

    @property
    def module_type(self) -> AIModuleType:
        return AIModuleType.QUERY_GENERATOR


class SchemaOptimiser(AIModuleInterface):
    """Takes a source schema, returns proposed meta-schema improvements."""

    class Input(BaseModel):
        """Input for schema optimisation."""

        source_name: str = Field(..., description="Source name")
        source_schema: dict[str, Any] = Field(..., description="Current schema definition")
        query_patterns: list[str] = Field(
            default_factory=list,
            description="Common query patterns against this source",
        )
        context: dict[str, Any] = Field(default_factory=dict, description="Additional context")

    class Output(BaseModel):
        """Output from schema optimisation."""

        proposed_columns: list[dict[str, Any]] = Field(
            default_factory=list,
            description="Proposed column additions/modifications",
        )
        proposed_indices: list[str] = Field(
            default_factory=list, description="Proposed index changes"
        )
        rationale: str = Field(default="", description="Explanation of proposals")

    @property
    def module_type(self) -> AIModuleType:
        return AIModuleType.SCHEMA_OPTIMISER


class LogParser(AIModuleInterface):
    """Takes raw log samples, returns parsers + proposed meta-schema."""

    class Input(BaseModel):
        """Input for log parsing."""

        samples: list[str] = Field(..., description="Raw log samples (strings)")
        source_hint: str | None = Field(
            default=None,
            description="Hint about log source (e.g. 'nginx', 'aws_cloudtrail')",
        )
        existing_schema: dict[str, Any] | None = Field(
            default=None, description="Existing schema to build upon"
        )

    class Output(BaseModel):
        """Output from log parsing."""

        parser_config: dict[str, Any] = Field(
            default_factory=dict,
            description="Parser configuration (Vector/custom format)",
        )
        proposed_schema: dict[str, Any] = Field(
            default_factory=dict,
            description="Proposed meta-schema for parsed fields",
        )
        field_mappings: dict[str, str] = Field(
            default_factory=dict,
            description="Detected field name -> canonical name mappings",
        )
        confidence: float = Field(
            default=0.0,
            description="Parser confidence score (0.0 to 1.0)",
        )

    @property
    def module_type(self) -> AIModuleType:
        return AIModuleType.LOG_PARSER


# -- Registry -----------------------------------------------


class AIModuleRegistry:
    """Discovery and management of AI modules.

    Modules register by name and can be retrieved by name or type.
    Thread-safe for registration; modules themselves handle concurrency.
    """

    def __init__(self) -> None:
        self._modules: dict[str, AIModuleInterface] = {}

    def register(self, module: AIModuleInterface) -> None:
        """Register an AI module by its name.

        Raises:
            ValueError: If a module with the same name is already registered.
        """
        if module.name in self._modules:
            raise ValueError(f"AI module '{module.name}' is already registered.")
        self._modules[module.name] = module
        logger.info(f"Registered AI module '{module.name}' (type={module.module_type.value})")

    def unregister(self, name: str) -> None:
        """Remove a registered module."""
        self._modules.pop(name, None)

    def get(self, name: str) -> AIModuleInterface:
        """Get a module by name.

        Raises:
            KeyError: If no module with that name is registered.
        """
        if name not in self._modules:
            raise KeyError(
                f"AI module '{name}' not found. Registered: {list(self._modules.keys())}"
            )
        return self._modules[name]

    def list_modules(self, module_type: AIModuleType | None = None) -> list[AIModuleInterface]:
        """List registered modules, optionally filtered by type."""
        modules = list(self._modules.values())
        if module_type is not None:
            modules = [m for m in modules if m.module_type == module_type]
        return modules

    @property
    def module_names(self) -> list[str]:
        """Names of all registered modules."""
        return list(self._modules.keys())
