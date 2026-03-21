"""
QueryResult - Arrow-native result container with export utilities.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pyarrow as pa
from pyarrow import ipc

from dfe_engine.query.models import ExplainPlan, QueryMetadata

if TYPE_CHECKING:
    import pandas as pd


class QueryResult:
    """
    Unified query result container.

    Internally holds Arrow data, provides zero-copy exports to multiple formats.

    Attributes:
        table: The underlying PyArrow Table
        metadata: Query execution metadata
        explain: Optional EXPLAIN plan (if requested)
    """

    __slots__ = ("_explain", "_metadata", "_table")

    def __init__(
        self,
        table: pa.Table,
        metadata: QueryMetadata,
        explain: ExplainPlan | None = None,
    ):
        self._table = table
        self._metadata = metadata
        self._explain = explain

    @property
    def table(self) -> pa.Table:
        """Access the underlying Arrow Table."""
        return self._table

    @property
    def metadata(self) -> QueryMetadata:
        """Query execution metadata."""
        return self._metadata

    @property
    def explain(self) -> ExplainPlan | None:
        """EXPLAIN plan if requested."""
        return self._explain

    @property
    def num_rows(self) -> int:
        """Number of rows in result."""
        return self._table.num_rows

    @property
    def num_columns(self) -> int:
        """Number of columns in result."""
        return self._table.num_columns

    @property
    def schema(self) -> pa.Schema:
        """Arrow schema of result."""
        return self._table.schema

    @property
    def column_names(self) -> list[str]:
        """List of column names."""
        return self._table.column_names

    # -------------------------------------------------------------------------
    # Export Methods
    # -------------------------------------------------------------------------

    def to_arrow(self) -> pa.Table:
        """Return Arrow Table (zero-copy)."""
        return self._table

    def to_arrow_ipc(self, include_explain: bool = True) -> bytes:
        """
        Serialize to Arrow IPC stream format.

        Args:
            include_explain: Embed EXPLAIN plan in schema metadata

        Returns:
            Arrow IPC stream bytes
        """
        schema = self._table.schema

        # Embed explain plan in schema metadata if available
        if include_explain and self._explain:
            existing_metadata = schema.metadata or {}
            explain_metadata = self._explain.to_arrow_metadata()
            combined = {
                **{k.encode(): v.encode() for k, v in explain_metadata.items()},
                **existing_metadata,
            }
            schema = schema.with_metadata(combined)

        sink = pa.BufferOutputStream()
        with ipc.new_stream(sink, schema) as writer:
            writer.write_table(self._table.cast(schema))
        return sink.getvalue().to_pybytes()

    def to_pandas(self, **kwargs: Any) -> pd.DataFrame:
        """
        Convert to pandas DataFrame (zero-copy where possible).

        Args:
            **kwargs: Passed to pyarrow.Table.to_pandas()

        Returns:
            pandas DataFrame
        """
        return self._table.to_pandas(**kwargs)

    def to_pylist(self) -> list[dict[str, Any]]:
        """Convert to list of dictionaries."""
        return self._table.to_pylist()

    def to_pydict(self) -> dict[str, list[Any]]:
        """Convert to dictionary of columns."""
        return self._table.to_pydict()

    def to_json(self, **kwargs: Any) -> str:
        """
        Convert to JSON string.

        Args:
            **kwargs: Passed to json.dumps()

        Returns:
            JSON string
        """
        import json

        return json.dumps(self.to_pylist(), default=str, **kwargs)

    def to_csv(self, include_header: bool = True) -> str:
        """
        Convert to CSV string.

        Args:
            include_header: Include column names as first row

        Returns:
            CSV string
        """
        import csv

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=self.column_names)
        if include_header:
            writer.writeheader()
        writer.writerows(self.to_pylist())
        return output.getvalue()

    def to_parquet(self, path: str, **kwargs: Any) -> None:
        """
        Write to Parquet file.

        Args:
            path: Output file path
            **kwargs: Passed to pyarrow.parquet.write_table()
        """
        import pyarrow.parquet as pq

        pq.write_table(self._table, path, **kwargs)

    # -------------------------------------------------------------------------
    # Iteration
    # -------------------------------------------------------------------------

    def iter_batches(self, batch_size: int = 10_000) -> Iterator[pa.RecordBatch]:
        """
        Iterate over result in batches.

        Args:
            batch_size: Maximum rows per batch

        Yields:
            Arrow RecordBatch
        """
        yield from self._table.to_batches(max_chunksize=batch_size)

    def iter_rows(self) -> Iterator[dict[str, Any]]:
        """Iterate over rows as dictionaries."""
        yield from self.to_pylist()

    # -------------------------------------------------------------------------
    # Dunder Methods
    # -------------------------------------------------------------------------

    def __len__(self) -> int:
        return self.num_rows

    def __repr__(self) -> str:
        return (
            f"QueryResult(rows={self.num_rows}, cols={self.num_columns}, "
            f"duration={self._metadata.query_duration_ms}ms)"
        )

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return self.iter_rows()

    # -------------------------------------------------------------------------
    # Class Methods
    # -------------------------------------------------------------------------

    @classmethod
    def from_arrow_ipc(
        cls,
        data: bytes,
        metadata: QueryMetadata,
    ) -> QueryResult:
        """
        Construct QueryResult from Arrow IPC bytes.

        Args:
            data: Arrow IPC stream bytes
            metadata: Query metadata

        Returns:
            QueryResult instance
        """
        reader = ipc.open_stream(data)
        table = reader.read_all()

        # Extract explain plan from schema metadata if present
        explain = None
        if table.schema.metadata:
            explain = ExplainPlan.from_arrow_metadata(table.schema.metadata)

        return cls(table=table, metadata=metadata, explain=explain)
