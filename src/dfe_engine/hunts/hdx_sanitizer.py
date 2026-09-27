"""HyperDX SQL sanitizer -- strip HyperDX-generated patterns from SQL.

HyperDX generates SQL with patterns that are not part of user detection
logic: epoch-millis time bounds, time-bucket columns, SETTINGS clauses,
and LIMIT/OFFSET.  This module strips those patterns, leaving only the
user's detection logic for reuse as a hunt Rule.

Patterns stripped (from HyperDX ``renderChartConfig.ts``):

1. ``fromUnixTimestamp64Milli(...)`` time bounds in WHERE
2. ``toDate(fromUnixTimestamp64Milli(...))`` Date column variant
3. ``toStartOfInterval(fromUnixTimestamp64Milli(...), INTERVAL ...) +/- INTERVAL`` compound bounds
4. ``toStartOfInterval(toDateTime(...), INTERVAL ...) AS __hdx_time_bucket`` in SELECT
5. ``__hdx_time_bucket`` references in GROUP BY, ORDER BY
6. ``SETTINGS ...`` clause
7. ``LIMIT ... OFFSET ...`` clause

Usage::

    from dfe_engine.hunts.hdx_sanitizer import HdxSanitizer

    sanitizer = HdxSanitizer()
    result = sanitizer.sanitize(raw_sql)
    # result.clean_sql  -- SQL with HyperDX patterns removed
    # result.stripped_time_bounds  -- patterns that were removed
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class HdxSanitizeResult:
    """Result of sanitizing HyperDX SQL."""

    clean_sql: str = ""
    stripped_time_bounds: list[str] = field(default_factory=list)
    stripped_settings: str | None = None
    stripped_limit: str | None = None
    stripped_time_bucket_select: list[str] = field(default_factory=list)
    stripped_time_bucket_refs: list[str] = field(default_factory=list)
    had_time_bucket: bool = False
    warnings: list[str] = field(default_factory=list)


class HdxSanitizer:
    """Strip HyperDX-generated SQL patterns.

    Processing order matters -- SETTINGS must be stripped first
    (always at end of SQL), then LIMIT, then structural patterns
    in SELECT/GROUP BY/ORDER BY, and finally WHERE time bounds.
    """

    # -- Compiled patterns ------------------------------------

    # SETTINGS clause: always at end of SQL
    _SETTINGS_RE = re.compile(
        r"\bSETTINGS\s+.+$",
        re.IGNORECASE | re.DOTALL,
    )

    # LIMIT [n] [OFFSET m]
    _LIMIT_RE = re.compile(
        r"\bLIMIT\s+\d+(?:\s+OFFSET\s+\d+)?",
        re.IGNORECASE,
    )

    # Time bucket in SELECT/GROUP BY/ORDER BY:
    #   toStartOfInterval(toDateTime(Col), INTERVAL N unit) AS `__hdx_time_bucket`
    # Matches exactly: toStartOfInterval(FUNC(arg), INTERVAL N unit) AS __hdx_time_bucket
    _TIME_BUCKET_EXPR_RE = re.compile(
        r"toStartOfInterval\s*\(\s*\w+\s*\([^)]*\)\s*,\s*INTERVAL\s+\w+\s+\w+\s*\)\s+AS\s+[`\"]?__hdx_time_bucket[`\"]?",
        re.IGNORECASE,
    )

    # Bare __hdx_time_bucket reference (in GROUP BY / ORDER BY)
    _HDX_BUCKET_REF_RE = re.compile(
        r"[`\"]?__hdx_time_bucket[`\"]?",
        re.IGNORECASE,
    )

    # Time bounds using fromUnixTimestamp64Milli:
    #   col >= fromUnixTimestamp64Milli(123)
    #   col <= toDate(fromUnixTimestamp64Milli(456))
    # Two alternatives so the closing paren is only consumed with toDate()
    _EPOCH_TIME_BOUND_RE = re.compile(
        r"""\b\w+\s*(?:>=?|<=?|=)\s*
        (?:
            toDate\s*\(\s*fromUnixTimestamp64Milli\s*\(\s*\d+\s*\)\s*\)   # toDate() variant
          | fromUnixTimestamp64Milli\s*\(\s*\d+\s*\)                       # bare variant
        )
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # Compound time bounds:
    #   toStartOfInterval(fromUnixTimestamp64Milli(N), INTERVAL M unit) [+/- INTERVAL M unit]
    # These appear as RHS of comparisons -- match the full expression
    _COMPOUND_TIME_BOUND_RE = re.compile(
        r"""\b\w+\s*(?:>=?|<=?|=)\s*
        toStartOfInterval\s*\(\s*
        fromUnixTimestamp64Milli\s*\(\s*\d+\s*\)\s*,\s*
        INTERVAL\s+\w+(?:\s+\w+)?\s*\)
        (?:\s*[+-]\s*INTERVAL\s+\w+(?:\s+\w+)?)?
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # -- Public API -------------------------------------------

    def sanitize(self, sql: str) -> HdxSanitizeResult:
        """Strip all HyperDX patterns from SQL.

        Args:
            sql: Raw SQL from HyperDX.

        Returns:
            HdxSanitizeResult with cleaned SQL and metadata about
            what was stripped.
        """
        result = HdxSanitizeResult()
        text = sql.strip()

        if not text:
            return result

        # Phase 1: SETTINGS (always at end -- remove first)
        text = self._strip_settings(text, result)

        # Phase 2: LIMIT/OFFSET
        text = self._strip_limit(text, result)

        # Phase 3: time bucket expressions in SELECT, GROUP BY, ORDER BY
        text = self._strip_time_bucket_exprs(text, result)

        # Phase 4: bare __hdx_time_bucket references in GROUP BY, ORDER BY
        text = self._strip_bucket_refs(text, result)

        # Phase 5: compound time bounds in WHERE (must come before simple)
        text = self._strip_compound_time_bounds(text, result)

        # Phase 6: simple epoch time bounds in WHERE
        text = self._strip_epoch_time_bounds(text, result)

        # Phase 7: clean up residual artifacts
        text = self._clean_residual(text)

        result.clean_sql = text.strip()
        return result

    # -- Internal methods -------------------------------------

    def _strip_settings(self, text: str, result: HdxSanitizeResult) -> str:
        m = self._SETTINGS_RE.search(text)
        if m:
            result.stripped_settings = m.group(0).strip()
            text = text[: m.start()].rstrip()
        return text

    def _strip_limit(self, text: str, result: HdxSanitizeResult) -> str:
        m = self._LIMIT_RE.search(text)
        if m:
            result.stripped_limit = m.group(0).strip()
            text = text[: m.start()] + text[m.end() :]
        return text

    def _strip_time_bucket_exprs(self, text: str, result: HdxSanitizeResult) -> str:
        for m in self._TIME_BUCKET_EXPR_RE.finditer(text):
            result.stripped_time_bucket_select.append(m.group(0).strip())
            result.had_time_bucket = True
        text = self._TIME_BUCKET_EXPR_RE.sub("", text)
        return text

    def _strip_bucket_refs(self, text: str, result: HdxSanitizeResult) -> str:
        for m in self._HDX_BUCKET_REF_RE.finditer(text):
            result.stripped_time_bucket_refs.append(m.group(0).strip())
            result.had_time_bucket = True
        text = self._HDX_BUCKET_REF_RE.sub("", text)
        return text

    def _strip_compound_time_bounds(self, text: str, result: HdxSanitizeResult) -> str:
        for m in self._COMPOUND_TIME_BOUND_RE.finditer(text):
            result.stripped_time_bounds.append(m.group(0).strip())
        text = self._COMPOUND_TIME_BOUND_RE.sub("", text)
        return text

    def _strip_epoch_time_bounds(self, text: str, result: HdxSanitizeResult) -> str:
        for m in self._EPOCH_TIME_BOUND_RE.finditer(text):
            result.stripped_time_bounds.append(m.group(0).strip())
        text = self._EPOCH_TIME_BOUND_RE.sub("", text)
        return text

    @staticmethod
    def _clean_residual(text: str) -> str:
        """Clean dangling commas, empty clauses, and boolean artifacts."""
        # Dangling commas in SELECT: "SELECT ,col" or "col, , col" or "col, FROM"
        text = re.sub(r",\s*,", ",", text)
        text = re.sub(r"\bSELECT\s*,", "SELECT ", text, flags=re.IGNORECASE)
        text = re.sub(r",\s*FROM\b", " FROM", text, flags=re.IGNORECASE)

        # Dangling commas in GROUP BY / ORDER BY
        text = re.sub(
            r"(GROUP\s+BY|ORDER\s+BY)\s*,",
            r"\1 ",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r",\s*(?=\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|\bSETTINGS\b|\bHAVING\b|$)",
            " ",
            text,
            flags=re.IGNORECASE,
        )

        # Boolean cleanup in WHERE
        text = re.sub(r"\(\s*AND\s+", "(", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+AND\s*\)", ")", text, flags=re.IGNORECASE)
        text = re.sub(r"\bAND\s+AND\b", "AND", text, flags=re.IGNORECASE)
        text = re.sub(r"\bOR\s+OR\b", "OR", text, flags=re.IGNORECASE)

        # Remove empty parentheses -- but NOT after identifiers (function calls)
        # e.g. remove standalone "()" but keep "count()" and "countIf()"
        text = re.sub(r"(?<!\w)\(\s*\)", "", text)

        # Leading AND/OR after WHERE (after paren cleanup may expose these)
        text = re.sub(r"\bWHERE\s+AND\b", "WHERE", text, flags=re.IGNORECASE)
        text = re.sub(r"\bWHERE\s+OR\b", "WHERE", text, flags=re.IGNORECASE)

        # Remove empty WHERE clause (may contain leftover parens or whitespace)
        text = re.sub(
            r"\bWHERE\s*\(\s*\)\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\bWHERE\s*(?=\bGROUP\b|\bORDER\b|\bLIMIT\b|\bSETTINGS\b|\bHAVING\b|$)",
            "",
            text,
            flags=re.IGNORECASE,
        )

        # Remove empty GROUP BY / ORDER BY
        text = re.sub(
            r"\bGROUP\s+BY\s*(?=\bORDER\b|\bLIMIT\b|\bSETTINGS\b|\bHAVING\b|$)",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\bORDER\s+BY\s*(?=\bLIMIT\b|\bSETTINGS\b|$)",
            "",
            text,
            flags=re.IGNORECASE,
        )

        # Collapse multiple whitespace
        text = re.sub(r"[ \t]+", " ", text)

        return text.strip()
