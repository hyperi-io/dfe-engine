"""Unit tests for QueryResult."""

import json

import pytest

from dfe_engine.query.models import (
    ExplainPlan,
    ExplainStep,
    ExplainStepType,
    QueryMetadata,
)
from dfe_engine.query.result import QueryResult


@pytest.fixture
def sample_rows():
    return [
        {"id": 1, "name": "Alice", "score": 85.5, "active": True},
        {"id": 2, "name": "Bob", "score": 92.0, "active": True},
        {"id": 3, "name": "Charlie", "score": 78.5, "active": False},
        {"id": 4, "name": "Diana", "score": 95.0, "active": True},
        {"id": 5, "name": "Eve", "score": 88.5, "active": False},
    ]


@pytest.fixture
def sample_columns():
    return ["id", "name", "score", "active"]


@pytest.fixture
def sample_metadata():
    return QueryMetadata(
        row_count=5,
        query_duration_ms=42,
        query_label="test/query",
        datasource="clickhouse:default",
    )


@pytest.fixture
def sample_explain():
    return ExplainPlan(
        steps=[
            ExplainStep(
                step_type=ExplainStepType.READ,
                description="ReadFromMergeTree (users)",
                estimated_rows=100,
            ),
            ExplainStep(
                step_type=ExplainStepType.PROJECTION,
                description="Project columns",
            ),
        ],
        warnings=["Consider adding index"],
        raw_plan="Expression...\n  ReadFromMergeTree...",
    )


@pytest.fixture
def sample_result(sample_rows, sample_columns, sample_metadata):
    return QueryResult(rows=sample_rows, columns=sample_columns, metadata=sample_metadata)


class TestQueryResultBasics:
    def test_create_result(self, sample_result):
        assert sample_result.num_rows == 5
        assert sample_result.num_columns == 4
        assert sample_result.column_names == ["id", "name", "score", "active"]

    def test_result_with_explain(
        self, sample_rows, sample_columns, sample_metadata, sample_explain
    ):
        result = QueryResult(
            rows=sample_rows,
            columns=sample_columns,
            metadata=sample_metadata,
            explain=sample_explain,
        )
        assert result.explain is not None
        assert len(result.explain.steps) == 2
        assert result.explain.warnings == ["Consider adding index"]

    def test_rows_property(self, sample_result, sample_rows):
        assert sample_result.rows == sample_rows

    def test_metadata_property(self, sample_result):
        assert sample_result.metadata.row_count == 5
        assert sample_result.metadata.query_duration_ms == 42
        assert sample_result.metadata.datasource == "clickhouse:default"


class TestQueryResultExports:
    def test_to_pylist(self, sample_result):
        rows = sample_result.to_pylist()
        assert len(rows) == 5
        assert rows[0] == {"id": 1, "name": "Alice", "score": 85.5, "active": True}
        assert rows[4] == {"id": 5, "name": "Eve", "score": 88.5, "active": False}

    def test_to_pydict(self, sample_result):
        cols = sample_result.to_pydict()
        assert list(cols.keys()) == ["id", "name", "score", "active"]
        assert cols["id"] == [1, 2, 3, 4, 5]
        assert cols["name"] == ["Alice", "Bob", "Charlie", "Diana", "Eve"]

    def test_to_json(self, sample_result):
        json_str = sample_result.to_json()
        data = json.loads(json_str)
        assert len(data) == 5
        assert data[0]["name"] == "Alice"

    def test_to_json_with_indent(self, sample_result):
        json_str = sample_result.to_json(indent=2)
        assert "\n" in json_str

    def test_to_csv(self, sample_result):
        csv_str = sample_result.to_csv()
        lines = csv_str.strip().replace("\r\n", "\n").split("\n")
        assert len(lines) == 6  # Header + 5 data rows
        assert lines[0] == "id,name,score,active"
        assert "Alice" in lines[1]

    def test_to_csv_without_header(self, sample_result):
        csv_str = sample_result.to_csv(include_header=False)
        lines = csv_str.strip().replace("\r\n", "\n").split("\n")
        assert len(lines) == 5
        assert "id" not in lines[0]

    def test_to_pandas(self, sample_result):
        df = sample_result.to_pandas()
        assert len(df) == 5
        assert list(df.columns) == ["id", "name", "score", "active"]
        assert df["name"].tolist() == ["Alice", "Bob", "Charlie", "Diana", "Eve"]


class TestQueryResultIteration:
    def test_iter_rows(self, sample_result):
        rows = list(sample_result.iter_rows())
        assert len(rows) == 5
        assert rows[0]["name"] == "Alice"
        assert rows[-1]["name"] == "Eve"

    def test_iter_dunder(self, sample_result):
        rows = list(sample_result)
        assert len(rows) == 5


class TestQueryResultDunder:
    def test_len(self, sample_result):
        assert len(sample_result) == 5

    def test_repr(self, sample_result):
        repr_str = repr(sample_result)
        assert "QueryResult" in repr_str
        assert "rows=5" in repr_str
        assert "cols=4" in repr_str
        assert "duration=42ms" in repr_str


class TestQueryResultEdgeCases:
    def test_empty_result(self, sample_metadata):
        result = QueryResult(rows=[], columns=["id"], metadata=sample_metadata)
        assert result.num_rows == 0
        assert len(result) == 0
        assert result.to_pylist() == []
        assert result.to_json() == "[]"

    def test_null_values(self, sample_metadata):
        rows = [
            {"id": 1, "name": "Alice", "score": 85.5},
            {"id": 2, "name": None, "score": None},
            {"id": 3, "name": "Charlie", "score": 78.5},
        ]
        result = QueryResult(rows=rows, columns=["id", "name", "score"], metadata=sample_metadata)
        out = result.to_pylist()
        assert out[1]["name"] is None
        assert out[1]["score"] is None

    def test_special_characters(self, sample_metadata):
        rows = [
            {"text": "Hello\nWorld"},
            {"text": "Tab\there"},
            {"text": 'Quote "test"'},
            {"text": "Unicode: 日本語"},
        ]
        result = QueryResult(rows=rows, columns=["text"], metadata=sample_metadata)
        out = result.to_pylist()
        assert out[0]["text"] == "Hello\nWorld"
        assert out[3]["text"] == "Unicode: 日本語"

        json_output = result.to_json(ensure_ascii=False)
        assert "日本語" in json_output
