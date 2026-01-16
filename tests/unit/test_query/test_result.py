"""Unit tests for QueryResult."""

import json

import pyarrow as pa
import pytest

from dfe_engine.query.models import (
    ExplainPlan,
    ExplainStep,
    ExplainStepType,
    QueryMetadata,
)
from dfe_engine.query.result import QueryResult


@pytest.fixture
def sample_arrow_table():
    """Create a sample Arrow table for testing."""
    return pa.table(
        {
            "id": [1, 2, 3, 4, 5],
            "name": ["Alice", "Bob", "Charlie", "Diana", "Eve"],
            "score": [85.5, 92.0, 78.5, 95.0, 88.5],
            "active": [True, True, False, True, False],
        }
    )


@pytest.fixture
def sample_metadata():
    """Create sample query metadata."""
    return QueryMetadata(
        row_count=5,
        query_duration_ms=42,
        query_label="test/query",
        datasource="clickhouse:default",
    )


@pytest.fixture
def sample_explain():
    """Create sample explain plan."""
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


class TestQueryResultBasics:
    """Test basic QueryResult functionality."""

    def test_create_result(self, sample_arrow_table, sample_metadata):
        """Test creating a QueryResult."""
        result = QueryResult(
            table=sample_arrow_table,
            metadata=sample_metadata,
        )

        assert result.num_rows == 5
        assert result.num_columns == 4
        assert result.column_names == ["id", "name", "score", "active"]

    def test_result_with_explain(self, sample_arrow_table, sample_metadata, sample_explain):
        """Test QueryResult with EXPLAIN plan."""
        result = QueryResult(
            table=sample_arrow_table,
            metadata=sample_metadata,
            explain=sample_explain,
        )

        assert result.explain is not None
        assert len(result.explain.steps) == 2
        assert result.explain.warnings == ["Consider adding index"]

    def test_table_property(self, sample_arrow_table, sample_metadata):
        """Test accessing the underlying Arrow table."""
        result = QueryResult(table=sample_arrow_table, metadata=sample_metadata)

        assert result.table is sample_arrow_table
        assert isinstance(result.table, pa.Table)

    def test_schema_property(self, sample_arrow_table, sample_metadata):
        """Test accessing the Arrow schema."""
        result = QueryResult(table=sample_arrow_table, metadata=sample_metadata)

        schema = result.schema
        assert isinstance(schema, pa.Schema)
        assert len(schema) == 4
        assert schema.field("id").type == pa.int64()
        assert schema.field("name").type == pa.string()

    def test_metadata_property(self, sample_arrow_table, sample_metadata):
        """Test accessing query metadata."""
        result = QueryResult(table=sample_arrow_table, metadata=sample_metadata)

        assert result.metadata.row_count == 5
        assert result.metadata.query_duration_ms == 42
        assert result.metadata.datasource == "clickhouse:default"


class TestQueryResultExports:
    """Test QueryResult export methods."""

    def test_to_arrow(self, sample_arrow_table, sample_metadata):
        """Test to_arrow() returns the table."""
        result = QueryResult(table=sample_arrow_table, metadata=sample_metadata)

        table = result.to_arrow()
        assert table is sample_arrow_table

    def test_to_pandas(self, sample_arrow_table, sample_metadata):
        """Test to_pandas() conversion."""
        result = QueryResult(table=sample_arrow_table, metadata=sample_metadata)

        df = result.to_pandas()

        assert len(df) == 5
        assert list(df.columns) == ["id", "name", "score", "active"]
        assert df["name"].tolist() == ["Alice", "Bob", "Charlie", "Diana", "Eve"]

    def test_to_pylist(self, sample_arrow_table, sample_metadata):
        """Test to_pylist() conversion."""
        result = QueryResult(table=sample_arrow_table, metadata=sample_metadata)

        rows = result.to_pylist()

        assert len(rows) == 5
        assert rows[0] == {"id": 1, "name": "Alice", "score": 85.5, "active": True}
        assert rows[4] == {"id": 5, "name": "Eve", "score": 88.5, "active": False}

    def test_to_pydict(self, sample_arrow_table, sample_metadata):
        """Test to_pydict() conversion."""
        result = QueryResult(table=sample_arrow_table, metadata=sample_metadata)

        cols = result.to_pydict()

        assert list(cols.keys()) == ["id", "name", "score", "active"]
        assert cols["id"] == [1, 2, 3, 4, 5]
        assert cols["name"] == ["Alice", "Bob", "Charlie", "Diana", "Eve"]

    def test_to_json(self, sample_arrow_table, sample_metadata):
        """Test to_json() conversion."""
        result = QueryResult(table=sample_arrow_table, metadata=sample_metadata)

        json_str = result.to_json()
        data = json.loads(json_str)

        assert len(data) == 5
        assert data[0]["name"] == "Alice"

    def test_to_json_with_indent(self, sample_arrow_table, sample_metadata):
        """Test to_json() with formatting options."""
        result = QueryResult(table=sample_arrow_table, metadata=sample_metadata)

        json_str = result.to_json(indent=2)

        assert "\n" in json_str  # Formatted output

    def test_to_csv(self, sample_arrow_table, sample_metadata):
        """Test to_csv() conversion."""
        result = QueryResult(table=sample_arrow_table, metadata=sample_metadata)

        csv_str = result.to_csv()

        # Handle both \r\n and \n line endings
        lines = csv_str.strip().replace("\r\n", "\n").split("\n")
        assert len(lines) == 6  # Header + 5 data rows
        assert lines[0] == "id,name,score,active"
        assert "Alice" in lines[1]

    def test_to_csv_without_header(self, sample_arrow_table, sample_metadata):
        """Test to_csv() without header."""
        result = QueryResult(table=sample_arrow_table, metadata=sample_metadata)

        csv_str = result.to_csv(include_header=False)

        # Handle both \r\n and \n line endings
        lines = csv_str.strip().replace("\r\n", "\n").split("\n")
        assert len(lines) == 5  # No header
        assert "id" not in lines[0]

    def test_to_parquet(self, sample_arrow_table, sample_metadata, tmp_path):
        """Test to_parquet() file export."""
        result = QueryResult(table=sample_arrow_table, metadata=sample_metadata)

        output_path = tmp_path / "output.parquet"
        result.to_parquet(str(output_path))

        # Verify file was created and is readable
        assert output_path.exists()

        import pyarrow.parquet as pq

        loaded = pq.read_table(str(output_path))
        assert loaded.num_rows == 5


class TestQueryResultArrowIPC:
    """Test Arrow IPC serialization/deserialization."""

    def test_to_arrow_ipc(self, sample_arrow_table, sample_metadata):
        """Test Arrow IPC serialization."""
        result = QueryResult(table=sample_arrow_table, metadata=sample_metadata)

        ipc_bytes = result.to_arrow_ipc()

        assert isinstance(ipc_bytes, bytes)
        assert len(ipc_bytes) > 0

        # Verify it can be deserialized
        from pyarrow import ipc

        reader = ipc.open_stream(ipc_bytes)
        loaded = reader.read_all()
        assert loaded.num_rows == 5

    def test_to_arrow_ipc_with_explain(self, sample_arrow_table, sample_metadata, sample_explain):
        """Test Arrow IPC includes EXPLAIN metadata."""
        result = QueryResult(
            table=sample_arrow_table,
            metadata=sample_metadata,
            explain=sample_explain,
        )

        ipc_bytes = result.to_arrow_ipc(include_explain=True)

        # Deserialize and check metadata
        from pyarrow import ipc

        reader = ipc.open_stream(ipc_bytes)
        loaded = reader.read_all()

        schema_metadata = loaded.schema.metadata
        assert schema_metadata is not None
        assert b"dfe:explain:steps" in schema_metadata
        assert b"dfe:explain:warnings" in schema_metadata

    def test_to_arrow_ipc_without_explain(
        self, sample_arrow_table, sample_metadata, sample_explain
    ):
        """Test Arrow IPC can exclude EXPLAIN metadata."""
        result = QueryResult(
            table=sample_arrow_table,
            metadata=sample_metadata,
            explain=sample_explain,
        )

        ipc_bytes = result.to_arrow_ipc(include_explain=False)

        from pyarrow import ipc

        reader = ipc.open_stream(ipc_bytes)
        loaded = reader.read_all()

        # No explain metadata should be present
        schema_metadata = loaded.schema.metadata
        assert schema_metadata is None or b"dfe:explain:steps" not in schema_metadata

    def test_from_arrow_ipc(self, sample_arrow_table, sample_metadata):
        """Test reconstructing QueryResult from Arrow IPC."""
        original = QueryResult(table=sample_arrow_table, metadata=sample_metadata)
        ipc_bytes = original.to_arrow_ipc()

        reconstructed = QueryResult.from_arrow_ipc(ipc_bytes, sample_metadata)

        assert reconstructed.num_rows == 5
        assert reconstructed.column_names == ["id", "name", "score", "active"]

    def test_from_arrow_ipc_with_explain(self, sample_arrow_table, sample_metadata, sample_explain):
        """Test reconstructing QueryResult with EXPLAIN from Arrow IPC."""
        original = QueryResult(
            table=sample_arrow_table,
            metadata=sample_metadata,
            explain=sample_explain,
        )
        ipc_bytes = original.to_arrow_ipc(include_explain=True)

        reconstructed = QueryResult.from_arrow_ipc(ipc_bytes, sample_metadata)

        assert reconstructed.explain is not None
        assert len(reconstructed.explain.steps) == 2
        assert reconstructed.explain.warnings == ["Consider adding index"]


class TestQueryResultIteration:
    """Test QueryResult iteration methods."""

    def test_iter_batches(self, sample_arrow_table, sample_metadata):
        """Test iterating over record batches."""
        result = QueryResult(table=sample_arrow_table, metadata=sample_metadata)

        batches = list(result.iter_batches())

        assert len(batches) >= 1
        total_rows = sum(b.num_rows for b in batches)
        assert total_rows == 5

    def test_iter_batches_with_size(self, sample_metadata):
        """Test batch iteration with custom batch size."""
        # Create larger table
        table = pa.table({"id": list(range(100))})
        result = QueryResult(table=table, metadata=sample_metadata)

        batches = list(result.iter_batches(batch_size=30))

        # Should have multiple batches
        assert len(batches) >= 3
        assert all(b.num_rows <= 30 for b in batches)

    def test_iter_rows(self, sample_arrow_table, sample_metadata):
        """Test iterating over rows."""
        result = QueryResult(table=sample_arrow_table, metadata=sample_metadata)

        rows = list(result.iter_rows())

        assert len(rows) == 5
        assert rows[0]["name"] == "Alice"
        assert rows[-1]["name"] == "Eve"

    def test_iter_dunder(self, sample_arrow_table, sample_metadata):
        """Test __iter__ protocol."""
        result = QueryResult(table=sample_arrow_table, metadata=sample_metadata)

        rows = list(result)  # Uses __iter__

        assert len(rows) == 5


class TestQueryResultDunder:
    """Test QueryResult dunder methods."""

    def test_len(self, sample_arrow_table, sample_metadata):
        """Test __len__ returns row count."""
        result = QueryResult(table=sample_arrow_table, metadata=sample_metadata)

        assert len(result) == 5

    def test_repr(self, sample_arrow_table, sample_metadata):
        """Test __repr__ is informative."""
        result = QueryResult(table=sample_arrow_table, metadata=sample_metadata)

        repr_str = repr(result)

        assert "QueryResult" in repr_str
        assert "rows=5" in repr_str
        assert "cols=4" in repr_str
        assert "duration=42ms" in repr_str


class TestQueryResultEdgeCases:
    """Test edge cases and special scenarios."""

    def test_empty_result(self, sample_metadata):
        """Test handling empty result set."""
        empty_table = pa.table({"id": pa.array([], type=pa.int64())})
        result = QueryResult(table=empty_table, metadata=sample_metadata)

        assert result.num_rows == 0
        assert len(result) == 0
        assert result.to_pylist() == []
        assert result.to_json() == "[]"

    def test_null_values(self, sample_metadata):
        """Test handling null values."""
        table = pa.table(
            {
                "id": [1, 2, 3],
                "name": ["Alice", None, "Charlie"],
                "score": [85.5, None, 78.5],
            }
        )
        result = QueryResult(table=table, metadata=sample_metadata)

        rows = result.to_pylist()
        assert rows[1]["name"] is None
        assert rows[1]["score"] is None

    def test_nested_types(self, sample_metadata):
        """Test handling nested/complex types."""
        table = pa.table(
            {
                "id": [1, 2],
                "tags": [["a", "b"], ["c"]],
                "metadata": [{"key": "value"}, {"key": "other"}],
            }
        )
        result = QueryResult(table=table, metadata=sample_metadata)

        rows = result.to_pylist()
        assert rows[0]["tags"] == ["a", "b"]
        assert rows[1]["metadata"] == {"key": "other"}

    def test_large_result(self, sample_metadata):
        """Test handling larger result sets."""
        n_rows = 100_000
        table = pa.table(
            {
                "id": list(range(n_rows)),
                "value": [f"value_{i}" for i in range(n_rows)],
            }
        )
        result = QueryResult(table=table, metadata=sample_metadata)

        assert result.num_rows == n_rows

        # Should be able to iterate efficiently
        count = 0
        for batch in result.iter_batches(batch_size=10_000):
            count += batch.num_rows
        assert count == n_rows

    def test_special_characters(self, sample_metadata):
        """Test handling special characters in data."""
        table = pa.table(
            {
                "text": [
                    "Hello\nWorld",
                    "Tab\there",
                    'Quote "test"',
                    "Unicode: 日本語 🎉",
                ],
            }
        )
        result = QueryResult(table=table, metadata=sample_metadata)

        rows = result.to_pylist()
        assert rows[0]["text"] == "Hello\nWorld"
        assert rows[3]["text"] == "Unicode: 日本語 🎉"

        # CSV should handle escaping
        csv_output = result.to_csv()
        assert "日本語" in csv_output

        # JSON should handle unicode (can be escaped or unescaped depending on ensure_ascii)
        json_output = result.to_json(ensure_ascii=False)
        assert "日本語" in json_output
