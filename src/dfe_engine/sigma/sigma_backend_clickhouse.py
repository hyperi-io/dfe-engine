#  Project:      dfe-engine
#  File:         sigma/sigma_backend_clickhouse.py
#  Purpose:      Sigma -> ClickHouse WHERE condition over the pysigma-backend-clickhouse backend
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The Sigma to ClickHouse backend: pysigma-backend-clickhouse, rendering a bare condition.

The package owns the Sigma semantics (modifiers, wildcards, LIKE and regex escaping,
CIDR, lists, negation). This subclass adds what the engine decides: the condition
alone with no ``SELECT``, field names rendered through the engine's column quoting,
and a ``lower(col) LIKE`` form for text-indexed columns.
"""

from typing import Any, ClassVar

from sigma.backends.clickhouse.clickhouse import ClickhouseBackend
from sigma.conditions import ConditionFieldEqualsValueExpression
from sigma.conversion.deferred import DeferredQueryExpression
from sigma.conversion.state import ConversionState
from sigma.correlations import SigmaCorrelationRule
from sigma.processing.pipeline import ProcessingItem, ProcessingPipeline
from sigma.processing.transformations import ConvertTypeTransformation
from sigma.rule import SigmaRule
from sigma.types import SigmaString

from dfe_engine.clickhouse.quoting import column_reference
from dfe_engine.source.type_registry import current_use_case

# The use cases dfe-schemas renders as a ClickHouse text index, so a LIKE over
# the indexed expression prunes granules. The templates are in schema_ddl
# _INDEX_TEMPLATES, and the other use cases render index types LIKE cannot read.
TEXT_INDEX_USE_CASES: frozenset[str] = frozenset({"word_search", "substring_search"})

# ClickHouse lower() folds ASCII only, so the needle folds the same way.
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def declares_text_index(field_meta: dict[str, Any]) -> bool:
    """Whether a column's metadata declares a use case rendered as a text index.

    Args:
        field_meta: One column's entry from a schema-metadata mapping.

    Returns:
        True when the declared use case renders a text index.
    """
    return current_use_case(field_meta.get("use_case")) in TEXT_INDEX_USE_CASES


class SqlBackend(ClickhouseBackend):
    """ClickHouse backend that renders a rule's detection as a bare WHERE condition."""

    # A correlation rule is a whole query, not a condition, so it has no place here.
    correlation_methods: ClassVar[dict[str, str] | None] = None

    # A JSON-derived column is a Dynamic value, which throws on `= 4688` once a row holds that
    # field as a string. A quoted number compares against either variant and a typed column.
    backend_processing_pipeline: ClassVar[ProcessingPipeline] = ProcessingPipeline(
        items=[ProcessingItem(transformation=ConvertTypeTransformation("str"))]
    )

    # isIPAddressInRange throws on a value that is not an address ('-', ''), which fails
    # the whole hunt over one dirty row, and refuses a Dynamic column from a JSON path.
    cidr_expression: ClassVar[str | None] = (
        "(toIPv6OrNull(toString({field})) IS NOT NULL"
        " AND isIPAddressInRange(toString({field}), '{value}'))"
    )

    def __init__(
        self,
        processing_pipeline: ProcessingPipeline | None = None,
        schema_metadata: dict | None = None,
    ):
        """Initialize the backend.

        Args:
            processing_pipeline: A pySigma processing pipeline, if any.
            schema_metadata: Column metadata keyed by column name. A column whose
                use case renders a text index is matched through ``lower()``.
        """
        # No full-text column exists to search, so a keyword-only rule is refused.
        super().__init__(processing_pipeline, full_log_column=None)
        self.schema_metadata = schema_metadata or {}

    def escape_and_quote_field(self, field_name: str) -> str:
        """Render a Sigma field name as a ClickHouse column reference."""
        return column_reference(field_name)

    def finalize_query_default(
        self,
        rule: SigmaRule | SigmaCorrelationRule,
        query: str,
        index: int,
        state: ConversionState,
    ) -> str:
        """Return the condition alone, without the package's ``SELECT``."""
        return query

    def convert_condition_field_eq_val_str(
        self, cond: ConditionFieldEqualsValueExpression, state: ConversionState
    ) -> str | DeferredQueryExpression:
        """Match a text-indexed column through ``lower()``, anything else as the package does.

        ILIKE reads every granule, while ClickHouse prunes on a text index for LIKE
        only, so an indexed column folds case on both sides. A value with no wildcard
        or LIKE metacharacter stays an equality.
        """
        value = cond.value
        if (
            isinstance(value, SigmaString)
            and declares_text_index(self.schema_metadata.get(cond.field, {}))
            and (value.contains_special() or any(c in str(value) for c in "%_\\"))
        ):
            pattern = self.convert_value_str(value, state, no_quote=True)
            pattern = pattern.replace("\\", "\\\\").translate(_ASCII_LOWER)
            return f"lower({self.escape_and_quote_field(cond.field)}) LIKE '{pattern}'"
        return super().convert_condition_field_eq_val_str(cond, state)
