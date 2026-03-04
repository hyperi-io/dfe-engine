"""Tests for AI Module Interface — ABCs, models, and registry."""

import pytest

from pydantic import BaseModel

from dfe_engine.ai import (
    AIModuleInterface,
    AIModuleRegistry,
    AIModuleResult,
    AIModuleStatus,
    AIModuleType,
    LogParser,
    QueryOptimiser,
    SchemaOptimiser,
)


# ── Concrete test implementations ──────────────────────────


class StubQueryOptimiser(QueryOptimiser):
    """Minimal concrete implementation for testing."""

    def __init__(self, module_name: str = "stub-query-opt"):
        self._name = module_name
        self._tasks: dict[str, AIModuleResult] = {}

    @property
    def name(self) -> str:
        return self._name

    def submit(self, input_data: BaseModel) -> str:
        task_id = self.generate_task_id()
        self._tasks[task_id] = AIModuleResult(
            task_id=task_id,
            module_name=self.name,
            module_type=self.module_type,
            status=AIModuleStatus.COMPLETED,
            output={"proposed_query": "SELECT 1", "rationale": "simplified"},
        )
        return task_id

    def get_result(self, task_id: str) -> AIModuleResult:
        if task_id not in self._tasks:
            raise KeyError(f"Unknown task: {task_id}")
        return self._tasks[task_id]


class StubSchemaOptimiser(SchemaOptimiser):
    @property
    def name(self) -> str:
        return "stub-schema-opt"

    def submit(self, input_data: BaseModel) -> str:
        return self.generate_task_id()

    def get_result(self, task_id: str) -> AIModuleResult:
        return AIModuleResult(
            task_id=task_id,
            module_name=self.name,
            module_type=self.module_type,
            status=AIModuleStatus.PENDING,
        )


class StubLogParser(LogParser):
    @property
    def name(self) -> str:
        return "stub-log-parser"

    def submit(self, input_data: BaseModel) -> str:
        return self.generate_task_id()

    def get_result(self, task_id: str) -> AIModuleResult:
        return AIModuleResult(
            task_id=task_id,
            module_name=self.name,
            module_type=self.module_type,
            status=AIModuleStatus.PENDING,
        )


# ── Enums ──────────────────────────────────────────────────


class TestEnums:
    def test_module_types(self):
        assert AIModuleType.QUERY_OPTIMISER == "query_optimiser"
        assert AIModuleType.SCHEMA_OPTIMISER == "schema_optimiser"
        assert AIModuleType.LOG_PARSER == "log_parser"

    def test_module_status(self):
        assert AIModuleStatus.PENDING == "pending"
        assert AIModuleStatus.RUNNING == "running"
        assert AIModuleStatus.COMPLETED == "completed"
        assert AIModuleStatus.FAILED == "failed"


# ── AIModuleResult ─────────────────────────────────────────


class TestAIModuleResult:
    def test_default_fields(self):
        r = AIModuleResult(
            task_id="t1",
            module_name="test",
            module_type=AIModuleType.QUERY_OPTIMISER,
        )
        assert r.status == AIModuleStatus.PENDING
        assert r.output == {}
        assert r.error is None
        assert r.duration_ms is None

    def test_completed_result(self):
        r = AIModuleResult(
            task_id="t2",
            module_name="test",
            module_type=AIModuleType.LOG_PARSER,
            status=AIModuleStatus.COMPLETED,
            output={"parser_config": {"type": "regex"}},
            duration_ms=150.5,
        )
        assert r.status == AIModuleStatus.COMPLETED
        assert r.output["parser_config"]["type"] == "regex"

    def test_failed_result(self):
        r = AIModuleResult(
            task_id="t3",
            module_name="test",
            module_type=AIModuleType.SCHEMA_OPTIMISER,
            status=AIModuleStatus.FAILED,
            error="Model timeout",
        )
        assert r.error == "Model timeout"

    def test_serialization(self):
        r = AIModuleResult(
            task_id="t4",
            module_name="test",
            module_type=AIModuleType.QUERY_OPTIMISER,
            status=AIModuleStatus.COMPLETED,
            output={"proposed_query": "SELECT 1"},
        )
        data = r.model_dump(mode="json")
        r2 = AIModuleResult.model_validate(data)
        assert r2.task_id == r.task_id
        assert r2.output == r.output


# ── Typed module ABCs ──────────────────────────────────────


class TestQueryOptimiser:
    def test_module_type(self):
        mod = StubQueryOptimiser()
        assert mod.module_type == AIModuleType.QUERY_OPTIMISER

    def test_submit_and_get_result(self):
        mod = StubQueryOptimiser()
        task_id = mod.submit(QueryOptimiser.Input(query="SELECT * FROM logs"))
        result = mod.get_result(task_id)
        assert result.status == AIModuleStatus.COMPLETED
        assert result.output["proposed_query"] == "SELECT 1"

    def test_task_id_format(self):
        mod = StubQueryOptimiser()
        task_id = mod.generate_task_id()
        assert task_id.startswith("stub-query-opt:")
        assert len(task_id.split(":")[1]) == 12

    def test_unknown_task_raises(self):
        mod = StubQueryOptimiser()
        with pytest.raises(KeyError):
            mod.get_result("nonexistent")

    def test_input_model(self):
        inp = QueryOptimiser.Input(
            query="SELECT count() FROM logs WHERE x = 1",
            execution_profile={"rows_read": 10000, "elapsed_ms": 250},
            source_table="logs",
        )
        assert "count()" in inp.query
        assert inp.execution_profile["rows_read"] == 10000

    def test_output_model(self):
        out = QueryOptimiser.Output(
            proposed_query="SELECT count() FROM logs PREWHERE x = 1",
            rationale="Moved filter to PREWHERE for fewer granule reads",
            estimated_improvement="2x fewer rows scanned",
        )
        assert "PREWHERE" in out.proposed_query


class TestSchemaOptimiser:
    def test_module_type(self):
        mod = StubSchemaOptimiser()
        assert mod.module_type == AIModuleType.SCHEMA_OPTIMISER

    def test_input_model(self):
        inp = SchemaOptimiser.Input(
            source_name="windows_audit",
            source_schema={"columns": [{"name": "event_id", "type": "integer"}]},
            query_patterns=["WHERE event_id = 4688"],
        )
        assert inp.source_name == "windows_audit"

    def test_output_model(self):
        out = SchemaOptimiser.Output(
            proposed_columns=[{"name": "process_name", "type": "string"}],
            proposed_indices=["INDEX idx_process_name process_name TYPE bloom_filter"],
            rationale="process_name is frequently filtered",
        )
        assert len(out.proposed_columns) == 1


class TestLogParser:
    def test_module_type(self):
        mod = StubLogParser()
        assert mod.module_type == AIModuleType.LOG_PARSER

    def test_input_model(self):
        inp = LogParser.Input(
            samples=[
                '{"timestamp": "2026-01-01", "level": "ERROR", "msg": "fail"}',
                '{"timestamp": "2026-01-02", "level": "INFO", "msg": "ok"}',
            ],
            source_hint="json_app_logs",
        )
        assert len(inp.samples) == 2

    def test_output_model(self):
        out = LogParser.Output(
            parser_config={"type": "json", "timestamp_key": "timestamp"},
            proposed_schema={"columns": [{"name": "level", "type": "string"}]},
            field_mappings={"msg": "message", "level": "severity"},
            confidence=0.95,
        )
        assert out.confidence == 0.95
        assert out.field_mappings["msg"] == "message"


# ── Registry ───────────────────────────────────────────────


class TestAIModuleRegistry:
    def test_register_and_get(self):
        reg = AIModuleRegistry()
        mod = StubQueryOptimiser()
        reg.register(mod)
        assert reg.get("stub-query-opt") is mod

    def test_duplicate_registration_raises(self):
        reg = AIModuleRegistry()
        reg.register(StubQueryOptimiser())
        with pytest.raises(ValueError, match="already registered"):
            reg.register(StubQueryOptimiser())

    def test_get_unknown_raises(self):
        reg = AIModuleRegistry()
        with pytest.raises(KeyError, match="not found"):
            reg.get("nonexistent")

    def test_unregister(self):
        reg = AIModuleRegistry()
        reg.register(StubQueryOptimiser())
        reg.unregister("stub-query-opt")
        with pytest.raises(KeyError):
            reg.get("stub-query-opt")

    def test_list_all_modules(self):
        reg = AIModuleRegistry()
        reg.register(StubQueryOptimiser())
        reg.register(StubSchemaOptimiser())
        reg.register(StubLogParser())
        assert len(reg.list_modules()) == 3

    def test_list_by_type(self):
        reg = AIModuleRegistry()
        reg.register(StubQueryOptimiser())
        reg.register(StubSchemaOptimiser())
        reg.register(StubLogParser())

        query_mods = reg.list_modules(AIModuleType.QUERY_OPTIMISER)
        assert len(query_mods) == 1
        assert query_mods[0].name == "stub-query-opt"

    def test_module_names(self):
        reg = AIModuleRegistry()
        reg.register(StubQueryOptimiser())
        reg.register(StubSchemaOptimiser())
        assert set(reg.module_names) == {"stub-query-opt", "stub-schema-opt"}

    def test_multiple_same_type(self):
        reg = AIModuleRegistry()
        reg.register(StubQueryOptimiser("opt-v1"))
        reg.register(StubQueryOptimiser("opt-v2"))
        assert len(reg.list_modules(AIModuleType.QUERY_OPTIMISER)) == 2

    def test_unregister_nonexistent_is_noop(self):
        reg = AIModuleRegistry()
        reg.unregister("does-not-exist")  # Should not raise
