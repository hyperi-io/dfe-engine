"""Unit tests for Query API models."""


import pytest

from dfe_engine.query.models import (
    ExplainPlan,
    ExplainStep,
    ExplainStepType,
    QueryMetadata,
    QueryOptions,
    QueryRequest,
)


class TestQueryRequest:
    """Tests for QueryRequest model (labeled query design)."""

    def test_minimal_request(self):
        """Test creating request with only required fields."""
        request = QueryRequest(
            query="analytics/user_activity",
        )

        assert request.query == "analytics/user_activity"
        assert request.params is None
        assert request.options is None

    def test_request_with_params(self):
        """Test request with query parameters."""
        request = QueryRequest(
            query="hunts/active_threats",
            params={"severities": ["critical", "high"]},
        )

        assert request.params == {"severities": ["critical", "high"]}

    def test_request_with_options(self):
        """Test request with all options."""
        options = QueryOptions(
            limit=100,
            timeout_seconds=60,
            include_explain=True,
            explain_parallel=True,
        )
        request = QueryRequest(
            query="analytics/events",
            options=options,
        )

        assert request.options.limit == 100
        assert request.options.timeout_seconds == 60
        assert request.options.include_explain is True
        assert request.options.explain_parallel is True

    def test_request_serialization(self):
        """Test JSON serialization."""
        request = QueryRequest(
            query="system/health",
            params={"id": 123},
        )

        data = request.model_dump()

        assert data["query"] == "system/health"
        assert data["params"] == {"id": 123}

    def test_various_query_labels(self):
        """Test different query label formats."""
        labels = [
            "analytics/user_activity",
            "hunts/active_threats",
            "system/health",
            "admin/table_stats",
        ]

        for label in labels:
            request = QueryRequest(query=label)
            assert request.query == label

    def test_invalid_query_label_format(self):
        """Test that invalid label formats are rejected."""
        invalid_labels = [
            "UPPER_CASE",
            "with spaces",
            "with-dashes",
            "",
        ]

        for label in invalid_labels:
            with pytest.raises(ValueError):
                QueryRequest(query=label)


class TestQueryOptions:
    """Tests for QueryOptions model."""

    def test_defaults(self):
        """Test default values."""
        options = QueryOptions()

        assert options.limit is None
        assert options.offset is None
        assert options.cursor is None
        assert options.after_key is None
        assert options.order_by is None
        assert options.order_dir == "asc"
        assert options.timeout_seconds is None
        assert options.include_explain is False
        assert options.explain_parallel is True
        assert options.cache is True
        assert options.store is None

    def test_limit_validation(self):
        """Test limit bounds validation."""
        # Valid limits
        QueryOptions(limit=1)
        QueryOptions(limit=100_000)

        # Invalid limits
        with pytest.raises(ValueError):
            QueryOptions(limit=0)

        with pytest.raises(ValueError):
            QueryOptions(limit=100_001)

    def test_timeout_validation(self):
        """Test timeout bounds validation."""
        # Valid timeouts
        QueryOptions(timeout_seconds=1)
        QueryOptions(timeout_seconds=300)

        # Invalid timeouts
        with pytest.raises(ValueError):
            QueryOptions(timeout_seconds=0)

        with pytest.raises(ValueError):
            QueryOptions(timeout_seconds=301)

    def test_pagination_offset_based(self):
        """Test offset-based pagination options."""
        options = QueryOptions(limit=100, offset=50)

        assert options.limit == 100
        assert options.offset == 50

    def test_pagination_cursor_based(self):
        """Test cursor-based pagination options."""
        options = QueryOptions(
            limit=100,
            cursor="eyJsYXN0X2lkIjogMTIzfQ==",
        )

        assert options.cursor == "eyJsYXN0X2lkIjogMTIzfQ=="

    def test_pagination_keyset_based(self):
        """Test keyset-based pagination options."""
        options = QueryOptions(
            limit=100,
            after_key="2024-01-15T12:00:00Z",
            order_by="timestamp",
            order_dir="desc",
        )

        assert options.after_key == "2024-01-15T12:00:00Z"
        assert options.order_by == "timestamp"
        assert options.order_dir == "desc"

    def test_time_bounds(self):
        """Test time bounds options."""
        options = QueryOptions(
            time_from="2024-01-01T00:00:00Z",
            time_to="2024-01-31T23:59:59Z",
        )

        assert options.time_from == "2024-01-01T00:00:00Z"
        assert options.time_to == "2024-01-31T23:59:59Z"


class TestQueryMetadata:
    """Tests for QueryMetadata model."""

    def test_basic_metadata(self):
        """Test creating basic metadata."""
        metadata = QueryMetadata(
            row_count=100,
            query_duration_ms=42,
            query_label="analytics/events",
            datasource="clickhouse:default",
        )

        assert metadata.row_count == 100
        assert metadata.query_duration_ms == 42
        assert metadata.query_label == "analytics/events"
        assert metadata.datasource == "clickhouse:default"
        assert metadata.truncated is False
        assert metadata.cached is False
        assert metadata.explain_duration_ms is None

    def test_full_metadata(self):
        """Test metadata with all fields."""
        metadata = QueryMetadata(
            row_count=1000,
            query_duration_ms=150,
            query_label="hunts/threats",
            datasource="clickhouse:analytics",
            store="events_db",
            truncated=True,
            cached=True,
            explain_duration_ms=25,
            has_more=True,
            next_cursor="eyJwYWdlIjogMn0=",
            next_offset=100,
            total_count=5000,
        )

        assert metadata.truncated is True
        assert metadata.cached is True
        assert metadata.explain_duration_ms == 25
        assert metadata.has_more is True
        assert metadata.next_cursor == "eyJwYWdlIjogMn0="
        assert metadata.next_offset == 100
        assert metadata.total_count == 5000

    def test_pagination_info_defaults(self):
        """Test pagination info defaults."""
        metadata = QueryMetadata(
            row_count=50,
            query_duration_ms=10,
            query_label="test/query",
            datasource="clickhouse:default",
        )

        assert metadata.has_more is False
        assert metadata.next_cursor is None
        assert metadata.next_offset is None
        assert metadata.total_count is None


class TestExplainStep:
    """Tests for ExplainStep model."""

    def test_basic_step(self):
        """Test creating basic explain step."""
        step = ExplainStep(
            step_type=ExplainStepType.READ,
            description="ReadFromMergeTree (logs)",
        )

        assert step.step_type == ExplainStepType.READ
        assert step.description == "ReadFromMergeTree (logs)"
        assert step.estimated_rows is None
        assert step.details is None

    def test_step_with_estimates(self):
        """Test step with row estimates."""
        step = ExplainStep(
            step_type=ExplainStepType.FILTER,
            description="Filter (level = 'ERROR')",
            estimated_rows=1000,
            estimated_cost=0.5,
        )

        assert step.estimated_rows == 1000
        assert step.estimated_cost == 0.5

    def test_step_with_details(self):
        """Test step with additional details."""
        step = ExplainStep(
            step_type=ExplainStepType.READ,
            description="ReadFromMergeTree",
            details={"table": "events", "parts": 42},
        )

        assert step.details["table"] == "events"
        assert step.details["parts"] == 42

    def test_all_step_types(self):
        """Test all step type enum values."""
        step_types = [
            ExplainStepType.READ,
            ExplainStepType.FILTER,
            ExplainStepType.AGGREGATE,
            ExplainStepType.SORT,
            ExplainStepType.JOIN,
            ExplainStepType.PROJECTION,
            ExplainStepType.LIMIT,
            ExplainStepType.UNION,
            ExplainStepType.UNKNOWN,
        ]

        for step_type in step_types:
            step = ExplainStep(step_type=step_type, description="test")
            assert step.step_type == step_type


class TestExplainPlan:
    """Tests for ExplainPlan model."""

    def test_basic_plan(self):
        """Test creating basic explain plan."""
        plan = ExplainPlan(
            steps=[
                ExplainStep(step_type=ExplainStepType.READ, description="Read"),
                ExplainStep(step_type=ExplainStepType.FILTER, description="Filter"),
            ],
        )

        assert len(plan.steps) == 2
        assert plan.warnings == []
        assert plan.raw_plan is None

    def test_plan_with_warnings(self):
        """Test plan with performance warnings."""
        plan = ExplainPlan(
            steps=[],
            warnings=["Full table scan", "Missing index"],
        )

        assert len(plan.warnings) == 2
        assert "Full table scan" in plan.warnings

    def test_plan_with_estimates(self):
        """Test plan with cost estimates."""
        plan = ExplainPlan(
            steps=[],
            total_estimated_cost=1.5,
            total_estimated_rows=10000,
        )

        assert plan.total_estimated_cost == 1.5
        assert plan.total_estimated_rows == 10000

    def test_to_dict(self):
        """Test conversion to dict for JSON responses."""
        plan = ExplainPlan(
            steps=[
                ExplainStep(
                    step_type=ExplainStepType.READ,
                    description="ReadFromMergeTree",
                    estimated_rows=1000,
                ),
            ],
            warnings=["Full scan"],
            total_estimated_cost=2.5,
            raw_plan="EXPLAIN output...",
        )

        data = plan.to_dict()

        assert "steps" in data
        assert len(data["steps"]) == 1
        assert data["warnings"] == ["Full scan"]
        assert data["total_estimated_cost"] == 2.5
        assert data["raw_plan"] == "EXPLAIN output..."
