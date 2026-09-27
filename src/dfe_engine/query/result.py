#  Project:      dfe-engine
#  File:         src/dfe_engine/query/result.py
#  Purpose:      Query result container with export utilities
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""QueryResult -- dict-native result container with export utilities."""

from __future__ import annotations

import io
from collections.abc import Iterator
from typing import Any

from dfe_engine.query.models import ExplainPlan, QueryMetadata


class QueryResult:
    """Unified query result container.

    Holds rows as a list of dicts (native clickhouse-connect output)
    and provides export to JSON, CSV, and pandas.

    Attributes:
        rows: List of row dicts
        columns: Column names
        metadata: Query execution metadata
        explain: Optional EXPLAIN plan
    """

    __slots__ = ("_columns", "_explain", "_metadata", "_rows")

    def __init__(
        self,
        rows: list[dict[str, Any]],
        columns: list[str],
        metadata: QueryMetadata,
        explain: ExplainPlan | None = None,
    ):
        self._rows = rows
        self._columns = columns
        self._metadata = metadata
        self._explain = explain

    @property
    def rows(self) -> list[dict[str, Any]]:
        """Access the result rows."""
        return self._rows

    @property
    def columns(self) -> list[str]:
        """Column names."""
        return self._columns

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
        return len(self._rows)

    @property
    def num_columns(self) -> int:
        """Number of columns in result."""
        return len(self._columns)

    @property
    def column_names(self) -> list[str]:
        """List of column names."""
        return self._columns

    # -- Export -----------------------------------------------

    def to_pylist(self) -> list[dict[str, Any]]:
        """Return rows as list of dicts."""
        return self._rows

    def to_pydict(self) -> dict[str, list[Any]]:
        """Return columns as dict of lists."""
        result: dict[str, list[Any]] = {col: [] for col in self._columns}
        for row in self._rows:
            for col in self._columns:
                result[col].append(row.get(col))
        return result

    def to_json(self, **kwargs: Any) -> str:
        """Serialise rows to JSON string."""
        import json

        return json.dumps(self._rows, default=str, **kwargs)

    def to_csv(self, include_header: bool = True) -> str:
        """Serialise rows to CSV string."""
        import csv

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=self._columns)
        if include_header:
            writer.writeheader()
        writer.writerows(self._rows)
        return output.getvalue()

    def to_pandas(self, **kwargs: Any):
        """Convert to pandas DataFrame."""
        import pandas as pd

        return pd.DataFrame(self._rows, columns=self._columns, **kwargs)

    # -- Iteration -------------------------------------------

    def iter_rows(self) -> Iterator[dict[str, Any]]:
        """Iterate over rows as dicts."""
        yield from self._rows

    def __len__(self) -> int:
        return self.num_rows

    def __repr__(self) -> str:
        return (
            f"QueryResult(rows={self.num_rows}, cols={self.num_columns}, "
            f"duration={self._metadata.query_duration_ms}ms)"
        )

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return self.iter_rows()
