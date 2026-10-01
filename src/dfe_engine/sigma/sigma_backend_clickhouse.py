import ipaddress
from typing import Any, ClassVar

from sigma.conditions import (
    ConditionAND,
    ConditionFieldEqualsValueExpression,
    ConditionItem,
    ConditionNOT,
    ConditionOR,
)
from sigma.conversion.base import TextQueryBackend
from sigma.conversion.deferred import DeferredQueryExpression
from sigma.conversion.state import ConversionState
from sigma.processing.pipeline import ProcessingPipeline
from sigma.rule import SigmaRule
from sigma.types import SigmaCIDRExpression, SigmaCompareExpression, SigmaNull, SigmaString

from ..clickhouse.quoting import column_reference as sql_field
from ..source.type_registry import current_use_case

# The use cases dfe-schemas renders as a ClickHouse text index, so a LIKE over
# the indexed expression prunes granules; the templates are in schema_ddl
# _INDEX_TEMPLATES, and the other use cases render index types LIKE cannot read.
TEXT_INDEX_USE_CASES: frozenset[str] = frozenset({"word_search", "substring_search"})


def declares_text_index(field_meta: dict[str, Any]) -> bool:
    """Whether a column's metadata declares a use case rendered as a text index.

    Args:
        field_meta: One column's entry from a schema-metadata mapping.

    Returns:
        True when the declared use case renders a text index.
    """
    return current_use_case(field_meta.get("use_case")) in TEXT_INDEX_USE_CASES


class SqlBackend(TextQueryBackend):
    """ClickHouse backend for translating Sigma rules to SQL-like queries."""

    name: ClassVar[str] = "clickhouse backend"
    formats: dict[str, str] = {
        "default": "Plain ClickHouse queries",
    }
    requires_pipeline: bool = False
    precedence: ClassVar[tuple[ConditionItem, ConditionItem, ConditionItem]] = (
        ConditionNOT,
        ConditionAND,
        ConditionOR,
    )
    parenthesize: bool = True
    group_expression: ClassVar[str] = "({expr})"

    token_separator: str = " "
    or_token: ClassVar[str] = "OR"
    and_token: ClassVar[str] = "AND"
    not_token: ClassVar[str] = "NOT"
    eq_token: ClassVar[str] = "="

    str_quote: ClassVar[str] = "'"
    escape_char: ClassVar[str] = "\\"
    wildcard_multi: ClassVar[str] = "*"
    wildcard_single: ClassVar[str] = "*"
    add_escaped: ClassVar[str] = "\\"
    filter_chars: ClassVar[str] = ""
    bool_values: ClassVar[dict[bool, str]] = {
        True: "true",
        False: "false",
    }

    startswith_expression: ClassVar[str] = "FIELD ILIKE 'VALUE%'"
    endswith_expression: ClassVar[str] = "FIELD ILIKE '%VALUE'"
    contains_expression: ClassVar[str] = "FIELD ILIKE '%VALUE%'"
    wildcard_match_expression: ClassVar[str] = "match"

    # Where the needle sits in the LIKE pattern, per anchoring.
    _LIKE_PATTERNS: ClassVar[dict[str, str]] = {
        "startswith": "{value}%",
        "endswith": "%{value}",
        "contains": "%{value}%",
    }

    re_expression: ClassVar[str] = "match(FIELD, 'REGEX')"
    re_escape_char: ClassVar[str] = "\\"
    # The regex sits inside a single-quoted literal, so the base-class re_expression
    # path must escape the single quote (with re_escape_escape_char also doubling the
    # backslash) or a crafted regex breaks out of the literal (F-SIGMA-ESCAPING). An
    # empty tuple escaped nothing. The overridden field-eq-regex path routes through
    # _create_match_expression -> _escape_value instead of this loop.
    re_escape: ClassVar[tuple[str, ...]] = ("'",)
    re_escape_escape_char: bool = True

    cidr_wildcard: ClassVar[str] = "*"
    cidr_expression: ClassVar[str] = "cidrmatch(FIELD, 'VALUE')"
    cidr_in_list_expression: ClassVar[str] = "FIELD in (VALUE)"

    compare_op_expression: ClassVar[str] = "FIELD OPERATOR VALUE"
    compare_operators: ClassVar[dict[SigmaCompareExpression.CompareOperators, str]] = {
        SigmaCompareExpression.CompareOperators.LT: "<",
        SigmaCompareExpression.CompareOperators.LTE: "<=",
        SigmaCompareExpression.CompareOperators.GT: ">",
        SigmaCompareExpression.CompareOperators.GTE: ">=",
    }

    field_null_expression: ClassVar[str] = "{field} is null"
    field_equals_field_expression: ClassVar[str] = "FIELD1 = FIELD2"

    convert_or_as_in: ClassVar[bool] = False
    convert_and_as_in: ClassVar[bool] = False
    in_expressions_allow_wildcards: ClassVar[bool] = True
    field_in_list_expression: ClassVar[str] = "FIELD OP (LIST)"
    or_in_operator: ClassVar[str] = "in"
    and_in_operator: ClassVar[str] = "contains-all"
    list_separator: ClassVar[str] = ", "

    unbound_value_str_expression: ClassVar[str] = "message ILIKE '%VALUE%'"
    unbound_value_num_expression: ClassVar[str] = "message ILIKE '%VALUE%'"
    unbound_value_re_expression: ClassVar[str] = "_=~VALUE"

    deferred_start: ClassVar[str] = "\n| "
    deferred_separator: ClassVar[str] = "\n| "
    deferred_only_query: ClassVar[str] = "*"

    last_processing_pipeline: ClassVar[ProcessingPipeline | None] = None

    def __init__(
        self,
        processing_pipeline: ProcessingPipeline | None = None,
        schema_metadata: dict | None = None,
    ):
        """Initialize the backend.

        Args:
            processing_pipeline: A pySigma processing pipeline, if any.
            schema_metadata: Column metadata keyed by column name; a column whose
                use case renders a text index is matched through ``lower()``.
        """
        super().__init__(processing_pipeline)
        self.schema_metadata = schema_metadata or {}

    def is_valid_cidr(self, cidr_str: str) -> bool:
        """Validate CIDR notation."""
        try:
            ipaddress.ip_network(cidr_str)
            return True
        except ValueError:
            return False

    def _escape_value(self, value: str) -> str:
        """Escape a value for a single-quoted ClickHouse string literal.

        Backslash MUST be doubled BEFORE the quote: ClickHouse honours C-style
        backslash escapes in string literals, so a lone ``\\'`` would otherwise
        break out of the literal (F-SIGMA-ESCAPING, same class of bug proven live
        for the row-policy predicate). Wildcards (``*``/``%``) are deliberately
        left untouched - they are literal-safe and their LIKE semantics are
        handled separately by _convert_wildcards. Every site that interpolates a
        value into ``'...'`` routes through here. ``value`` is coerced to ``str``
        first: some callers pass a ``SigmaString``/regexp object that the old
        f-string interpolation stringified implicitly.
        """
        return str(value).replace("\\", "\\\\").replace("'", "''")

    def _get_base_field_and_modifier(self, field_expr: str) -> tuple[str, str | None]:
        """Extract base field and modifier from field expression."""
        field_parts = field_expr.split("|")
        base_field = field_parts[0]
        modifier = field_parts[1] if len(field_parts) > 1 else None
        return base_field, modifier

    def _create_match_expression(self, field: str, value: str) -> str:
        """Create regex match expression for field and value.

        The regex is interpolated into a single-quoted literal, so it MUST be
        escaped (F-SIGMA-ESCAPING) - every _create_match_expression caller,
        including the regex condition path, relies on this single choke point.
        """
        return f"match({field}, '{self._escape_value(value)}')"

    def _create_cidr_expression(self, field: str, cidr: str) -> str:
        """Create CIDR match expression for field and value (escaped literal)."""
        return f"cidrmatch({field}, '{self._escape_value(cidr)}')"

    def _has_text_index(self, field: str) -> bool:
        """Whether *field* declares a use case dfe-schemas renders as a text index."""
        return declares_text_index(self.schema_metadata.get(field, {}))

    def _create_like_expression(
        self, field: str, value: str, pattern_type: str = "contains", indexed: bool = False
    ) -> str:
        """Create LIKE expression based on pattern type (startswith, endswith, contains).

        Args:
            field: Column name, already escaped and quoted.
            value: The needle, unescaped.
            pattern_type: startswith, endswith or contains; anything else reads as contains.
            indexed: Whether the column carries a text index.

        Returns:
            The SQL predicate.
        """
        pattern = self._LIKE_PATTERNS.get(pattern_type, self._LIKE_PATTERNS["contains"])
        if indexed:
            # ILIKE reads every granule: ClickHouse prunes on a text index for LIKE
            # only, so an indexed column folds case on both sides instead -- the shape
            # the shipped otel tables index (INDEX idx_lower_body lower(Body)).
            return (
                f"lower({field}) LIKE '{pattern.format(value=self._escape_value(value.lower()))}'"
            )
        return f"{field} ILIKE '{pattern.format(value=self._escape_value(value))}'"

    def _create_wildcard_expression(self, field: str, value: str, indexed: bool) -> str:
        """Match a value whose wildcards do not reduce to one of the LIKE patterns.

        F-SIGMA-ESCAPING: escape the SQL literal BEFORE mapping * -> %, so a crafted
        value (e.g. ``x*y' OR '1'='1``) cannot break out of the single-quoted literal.
        _escape_value leaves * untouched, so the wildcards still map afterwards.

        Args:
            field: Column name, already escaped and quoted.
            value: The needle, unescaped, carrying at least one ``*``.
            indexed: Whether the column carries a text index.

        Returns:
            The SQL predicate.
        """
        if indexed:
            literal = self._convert_wildcards(self._escape_value(value.lower()))
            return f"lower({field}) LIKE '{literal}'"
        return f"{field} ILIKE '{self._convert_wildcards(self._escape_value(value))}'"

    def _convert_wildcards(self, value: str) -> str:
        """Convert wildcards to SQL LIKE patterns."""
        if value.endswith("*") and not value.startswith("*") and value.count("*") == 1:
            return value[:-1]
        elif value.startswith("*") and not value.endswith("*") and value.count("*") == 1:
            return value[1:]
        elif value.startswith("*") and value.endswith("*") and value.count("*") == 2:
            return value[1:-1]

        result = ""
        i = 0
        while i < len(value):
            if value[i] == "*":
                result += "%"
            else:
                result += value[i]
            i += 1
        return result

    def _handle_field_value_expression(
        self, field: str, value: Any, modifier: str | None = None, raw_field: str | None = None
    ) -> str:
        """Render one field-value test.

        Args:
            field: The column reference, already rendered for SQL.
            value: The Sigma value.
            modifier: The value modifier (contains, startswith, ...), if any.
            raw_field: The column name before rendering, which the schema metadata
                is keyed by; defaults to ``field``.

        Returns:
            The SQL predicate.
        """
        if hasattr(value, "regexp"):
            return self._create_match_expression(field, value.regexp)

        # str() of a SigmaNull is its object address: a filter that never matches.
        if isinstance(value, SigmaNull):
            return self.field_null_expression.format(field=field)

        str_value = str(value)

        if isinstance(value, SigmaCIDRExpression):
            return self._create_cidr_expression(field, value.cidr)
        elif isinstance(value, SigmaString) and self.is_valid_cidr(str_value):
            return self._create_cidr_expression(field, str_value)

        name = field if raw_field is None else raw_field
        indexed = self._has_text_index(name)

        if name.lower().endswith("targetobject"):
            return self._create_like_expression(field, str_value, "contains", indexed)

        if modifier:
            if modifier == "endswith":
                return self._create_like_expression(field, str_value, "endswith", indexed)
            elif modifier == "startswith":
                return self._create_like_expression(field, str_value, "startswith", indexed)
            elif modifier == "contains":
                return self._create_like_expression(field, str_value, "contains", indexed)
            elif modifier == "re":
                return self._create_match_expression(field, str_value)

        if "*" in str_value:
            if (
                str_value.endswith("*")
                and not str_value.startswith("*")
                and str_value.count("*") == 1
            ):
                return self._create_like_expression(field, str_value[:-1], "startswith", indexed)
            elif (
                str_value.startswith("*")
                and not str_value.endswith("*")
                and str_value.count("*") == 1
            ):
                return self._create_like_expression(field, str_value[1:], "endswith", indexed)
            elif (
                str_value.startswith("*") and str_value.endswith("*") and str_value.count("*") == 2
            ):
                return self._create_like_expression(field, str_value[1:-1], "contains", indexed)
            else:
                return self._create_wildcard_expression(field, str_value, indexed)

        if hasattr(value, "source"):
            source = str(value.source)
            if "|contains" in source:
                return self._create_like_expression(field, str_value, "contains", indexed)
            elif "|endswith" in source:
                return self._create_like_expression(field, str_value, "endswith", indexed)
            elif "|startswith" in source:
                return self._create_like_expression(field, str_value, "startswith", indexed)
            elif "|re" in source:
                return self._create_match_expression(field, str_value)

        return f"{field} = '{self._escape_value(str_value)}'"

    def escape_and_quote_field(self, field_name: str) -> str:
        """Render a Sigma field name as a ClickHouse column reference."""
        return sql_field(field_name)

    def convert_condition_field_eq_val_num(
        self, cond: ConditionFieldEqualsValueExpression, state: ConversionState
    ) -> str:
        """Handle numeric values in field-equals-value expressions."""
        field = self.escape_and_quote_field(cond.field)
        value = cond.value

        if isinstance(value, list):
            values = [f"'{v}'" for v in value]
            return f"{field} IN ({', '.join(values)})"

        return f"{field} = '{value}'"

    def convert_condition_field_eq_val_bool(
        self, cond: ConditionFieldEqualsValueExpression, state: ConversionState
    ) -> str:
        """Handle boolean values in field-equals-value expressions."""
        field = self.escape_and_quote_field(cond.field)
        value = self.bool_values[cond.value]
        return f"{field} = {value}"

    def convert_condition_field_eq_val_re(
        self, cond: ConditionFieldEqualsValueExpression, state: ConversionState
    ) -> str:
        """Handle regular expressions in field-equals-value expressions."""
        field = self.escape_and_quote_field(cond.field)

        if hasattr(cond.value, "regexp"):
            return self._create_match_expression(field, cond.value.regexp)

        # cond.value is already a regex - do NOT regex-escape its meta-chars; only
        # the SQL-literal escaping applies, and _create_match_expression routes the
        # value through _escape_value (backslash + quote) for that (F-SIGMA-ESCAPING).
        return self._create_match_expression(field, str(cond.value))

    def convert_condition_field_eq_val_cidr(
        self, cond: ConditionFieldEqualsValueExpression, state: ConversionState
    ) -> str:
        """Handle CIDR expressions in field-equals-value expressions."""
        field = self.escape_and_quote_field(cond.field)
        value = cond.value

        if isinstance(value, list):
            cidr_expressions = []
            for cidr in value:
                if isinstance(cidr, SigmaCIDRExpression):
                    cidr_str = cidr.cidr
                else:
                    cidr_str = str(cidr)
                cidr_expressions.append(self._create_cidr_expression(field, cidr_str))
            return f"({' OR '.join(cidr_expressions)})"

        if isinstance(value, SigmaCIDRExpression):
            cidr_str = value.cidr
        else:
            cidr_str = str(value)

        return self._create_cidr_expression(field, cidr_str)

    def convert_value_str(
        self, s: SigmaString, state: ConversionState, no_quote: bool = True
    ) -> str:
        """Convert a SigmaString to a ClickHouse-compatible string."""
        raw_str = str(s)
        s = SigmaString(raw_str)
        converted = s.convert(self.wildcard_multi, self.wildcard_single, self.filter_chars)

        converted = self._escape_value(converted)

        if no_quote:
            return converted

        if self.decide_string_quoting(converted):
            return f"'{converted}'"
        return converted

    def _handle_list_values(
        self, field: str, value_list: list, cond, state: ConversionState
    ) -> str:
        """Handle list of values for a field."""
        base_field, modifier = self._get_base_field_and_modifier(cond.field)
        field = self.escape_and_quote_field(base_field)

        values = []
        for v in value_list:
            if isinstance(v, SigmaCIDRExpression) or (
                isinstance(v, SigmaString) and self.is_valid_cidr(str(v))
            ):
                cidr = v.cidr if isinstance(v, SigmaCIDRExpression) else str(v)
                values.append(self._create_cidr_expression(field, cidr))
            elif isinstance(v, SigmaString | SigmaNull):
                values.append(self._handle_field_value_expression(field, v, modifier, base_field))
            else:
                values.append(f"{field} = '{self._escape_value(str(v))}'")

        return f"({' OR '.join(values)})"

    def convert_condition_field_eq_val_str(
        self, cond: ConditionFieldEqualsValueExpression, state: ConversionState
    ) -> str | DeferredQueryExpression:
        """Converts field-equals-value conditions into ClickHouse-compatible SQL expressions."""
        try:
            value = cond.value
            base_field, modifier = self._get_base_field_and_modifier(cond.field)
            field = self.escape_and_quote_field(base_field)

            if isinstance(value, list):
                return self._handle_list_values(field, value, cond, state)

            return self._handle_field_value_expression(field, value, modifier, base_field)

        except Exception as e:
            raise NotImplementedError(f"Field equals string value expressions error: {e}")

    def convert_condition_and(
        self, cond: ConditionAND, state: ConversionState
    ) -> str | DeferredQueryExpression:
        """Convert AND condition with proper parentheses balancing."""
        try:
            converted_args = []
            for arg in cond.args:
                converted = self.convert_condition(arg, state)
                if " AND " in converted or " OR " in converted:
                    if not (converted.startswith("(") and converted.endswith(")")):
                        converted = f"({converted})"
                converted_args.append(converted)

            result = f" {self.and_token} ".join(converted_args)

            if len(cond.args) > 1:
                result = f"({result})"

            return result
        except TypeError:
            raise NotImplementedError("Operator 'and' not supported by the backend")

    def convert_condition_or(
        self, cond: ConditionOR, state: ConversionState
    ) -> str | DeferredQueryExpression:
        """Convert OR condition with proper parentheses balancing."""
        try:
            if all(isinstance(arg, ConditionFieldEqualsValueExpression) for arg in cond.args):
                if len(cond.args) > 0 and all(arg.field == cond.args[0].field for arg in cond.args):
                    base_field, modifier = self._get_base_field_and_modifier(cond.args[0].field)
                    field = self.escape_and_quote_field(base_field)
                    values = []

                    for arg in cond.args:
                        values.append(
                            self._handle_field_value_expression(
                                field, arg.value, modifier, base_field
                            )
                        )

                    return f"({' OR '.join(values)})"

            condition_str = str(cond)
            if condition_str.startswith("1 of filter_"):
                converted_args = []
                for arg in cond.args:
                    converted = self.convert_condition(arg, state)
                    if " AND " in converted or " OR " in converted:
                        if not (converted.startswith("(") and converted.endswith(")")):
                            converted = f"({converted})"
                    converted_args.append(converted)

                filter_result = f"({' OR '.join(converted_args)})"
                return filter_result

            converted_args = []
            for arg in cond.args:
                converted = self.convert_condition(arg, state)
                if " AND " in converted or " OR " in converted:
                    if not (converted.startswith("(") and converted.endswith(")")):
                        converted = f"({converted})"
                converted_args.append(converted)

            result = f" {self.or_token} ".join(converted_args)

            if len(cond.args) > 1:
                result = f"({result})"

            return result

        except TypeError:
            raise NotImplementedError("Operator 'or' not supported by the backend")

    def convert_condition_not(
        self, cond: ConditionNOT, state: ConversionState
    ) -> str | DeferredQueryExpression:
        """Convert NOT condition with proper parentheses balancing."""
        arg = cond.args[0]
        try:
            converted = self.convert_condition(arg, state)

            if not (converted.startswith("(") and converted.endswith(")")):
                converted = f"({converted})"

            return f"NOT {converted}"

        except TypeError:
            raise NotImplementedError("Operator 'not' not supported by the backend")

    def finalize_query_default(
        self, rule: SigmaRule, query: str, index: int, state: ConversionState
    ) -> Any:
        """Finalize query for default output format."""
        return query

    def finalize_output_default(self, queries: list[str]) -> Any:
        """Finalize output for default format."""
        return list(queries)
