"""Reduce the SQL behind a HyperDX view to the base query of a detection rule.

HyperDX renders every search and chart through ``renderChartConfig`` in the
dfe-hyperdx fork. A rule keeps what the user asked for: the source table and
every predicate from the search bar, the facet filters and the WHERE. It drops
what HyperDX adds to draw the view: the time-window bounds, the time buckets,
the series cap, aggregate projections, GROUP BY, ORDER BY, LIMIT/OFFSET,
SETTINGS, FORMAT and any wrapper subquery.

SQL that cannot be reduced that way without changing what the rule matches is
refused with ``HdxSanitizeError``: an unrendered template placeholder or macro,
HAVING, a join, a union, a CTE, a metric chart, or a time bound under OR or NOT.

User text is re-emitted token for token, never regenerated from a parse tree,
so the predicates the rule runs are the predicates the user wrote.

Usage::

    from dfe_engine.hunts.hdx_sanitizer import HdxSanitizer

    result = HdxSanitizer().sanitize(raw_sql)
    result.clean_sql  # SELECT <columns> FROM <table> WHERE <user predicates>
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<comment>--[^\n]*|/\*.*?\*/)
  | (?P<open_comment>/\*)
  | (?P<string>'(?:[^'\\]|\\.|'')*')
  | (?P<ident>`(?:[^`\\]|\\.|``)*`|"(?:[^"\\]|\\.|"")*")
  | (?P<macro>\$__\w+)
  | (?P<heredoc>\$(?P<tag>\w*)\$.*?\$(?P=tag)\$)
  | (?P<word>[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<number>\d[\w.]*)
  | (?P<op>->|::|<=|>=|!=|<>|==|\|\||.)
    """,
    re.VERBOSE | re.DOTALL,
)

_OPEN = frozenset({"(", "["})
_CLOSE = frozenset({")", "]"})
_WINDOW_OPS = frozenset({">=", ">", "<=", "<"})
_PREDICATE_WORDS = frozenset(
    {"NOT", "AND", "OR", "IN", "IS", "LIKE", "ILIKE", "BETWEEN", "NULL", "GLOBAL", "SELECT"}
)
_COMPARISON_OPS = _WINDOW_OPS | frozenset({"=", "==", "!=", "<>"})
_EPOCH_RE = re.compile(r"^fromUnixTimestamp\w*$", re.IGNORECASE)
_TO_FUNCTION_RE = re.compile(r"^to[A-Z]\w*$")
_HDX_NAME_RE = re.compile(r"^__hdx_")
_SERIES_CAP = "__hdx_series_limit"
_METRIC_CTES = frozenset({"Source", "Bucketed", "TopGroups"})

# ClickHouse clause order; clauses sharing a rank may appear in any order.
_CLAUSE_RANK = {
    "WITH": 0,
    "SELECT": 1,
    "FROM": 2,
    "PREWHERE": 3,
    "WHERE": 4,
    "GROUP BY": 5,
    "ORDER BY": 6,
    "LIMIT": 7,
    "OFFSET": 7,
    "FETCH": 7,
    "SETTINGS": 8,
    "INTO OUTFILE": 8,
    "FORMAT": 8,
}
_REFUSED_CLAUSES = {
    "HAVING": (
        "HAVING filters aggregated groups, and a rule matches single rows, so the "
        "rule would match more than the view shows. Remove the HAVING condition "
        "or express it as a row filter."
    ),
    "QUALIFY": "QUALIFY filters window-function results, which a rule cannot carry.",
    "WINDOW": "A WINDOW clause belongs to a window-function view, not a row filter.",
    "JOIN": "The query joins tables; a rule reads one source table.",
    "UNION": "The query unions several SELECTs; build one rule per branch.",
    "INTERSECT": "The query intersects several SELECTs; a rule reads one source table.",
    "EXCEPT": "The query subtracts one SELECT from another; a rule reads one source table.",
}
_SINGLE_WORD_CLAUSES = frozenset(
    {
        "WITH",
        "SELECT",
        "FROM",
        "PREWHERE",
        "WHERE",
        "HAVING",
        "WINDOW",
        "QUALIFY",
        "LIMIT",
        "OFFSET",
        "FETCH",
        "SETTINGS",
        "FORMAT",
        "JOIN",
        "UNION",
        "INTERSECT",
        "EXCEPT",
    }
)
_WITH_MODIFIERS = frozenset({"FILL", "TIES", "ROLLUP", "CUBE", "TOTALS"})

_AGGREGATES = frozenset(
    {
        "any",
        "anyheavy",
        "anylast",
        "argmax",
        "argmin",
        "avg",
        "avgweighted",
        "boundingratio",
        "corr",
        "count",
        "count_distinct",
        "covarpop",
        "covarsamp",
        "deltasum",
        "deltasumtimestamp",
        "entropy",
        "exponentialmovingaverage",
        "first_value",
        "grouparray",
        "grouparraymovingsum",
        "grouparraysample",
        "groupbitand",
        "groupbitmap",
        "groupbitor",
        "groupbitxor",
        "groupuniqarray",
        "histogram",
        "kurtpop",
        "kurtsamp",
        "last_value",
        "max",
        "maxintersections",
        "maxmap",
        "median",
        "medianexact",
        "mediantdigest",
        "mediantiming",
        "min",
        "minmap",
        "quantile",
        "quantilebfloat16",
        "quantileddsketch",
        "quantiledeterministic",
        "quantileexact",
        "quantileexactweighted",
        "quantilegk",
        "quantiles",
        "quantiletdigest",
        "quantiletdigestweighted",
        "quantiletiming",
        "quantiletimingweighted",
        "rankcorr",
        "retention",
        "sequencecount",
        "sequencematch",
        "simplelinearregression",
        "skewpop",
        "skewsamp",
        "stddevpop",
        "stddevsamp",
        "sum",
        "sumkahan",
        "summap",
        "sumwithoverflow",
        "topk",
        "topkweighted",
        "uniq",
        "uniqcombined",
        "uniqcombined64",
        "uniqexact",
        "uniqhll12",
        "uniqtheta",
        "varpop",
        "varsamp",
        "windowfunnel",
    }
)
_COMBINATORS = (
    "simplestate",
    "mergestate",
    "resample",
    "distinct",
    "ordefault",
    "ornull",
    "foreach",
    "argmin",
    "argmax",
    "array",
    "state",
    "merge",
    "map",
    "if",
)


class HdxSanitizeError(ValueError):
    """The SQL cannot become a rule base without changing what the rule matches."""


@dataclass(slots=True)
class HdxSanitizeResult:
    """The rule base and a record of what was removed to reach it.

    Attributes:
        clean_sql: ``SELECT <columns> FROM <table> [WHERE <user predicates>]``.
        stripped_time_bounds: Each time-window comparison removed.
        stripped_settings: The SETTINGS clause removed, if any.
        stripped_limit: The LIMIT/OFFSET/FETCH clauses removed, if any.
        stripped_time_bucket_select: Time-bucket columns removed from SELECT.
        stripped_time_bucket_refs: Time-bucket keys removed from GROUP BY / ORDER BY.
        had_time_bucket: Whether the view was bucketed by time.
        stripped_clauses: Every other removed piece: aggregates, GROUP BY,
            ORDER BY, FORMAT, the series cap and wrapper subqueries.
        warnings: Things the caller should tell the user about the rule.
    """

    clean_sql: str = ""
    stripped_time_bounds: list[str] = field(default_factory=list)
    stripped_settings: str | None = None
    stripped_limit: str | None = None
    stripped_time_bucket_select: list[str] = field(default_factory=list)
    stripped_time_bucket_refs: list[str] = field(default_factory=list)
    had_time_bucket: bool = False
    stripped_clauses: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Tok:
    kind: str
    text: str
    spaced: bool

    @property
    def keyword(self) -> str:
        return self.text.upper() if self.kind == "word" else ""


@dataclass(frozen=True, slots=True)
class _Clause:
    name: str
    head: list[_Tok]
    body: list[_Tok]


@dataclass(frozen=True, slots=True)
class _Pruned:
    text: str
    bare_or: bool


@dataclass(slots=True)
class _Reduced:
    projection: list[str]
    table: str
    where: str | None


def _tokenize(sql: str) -> list[_Tok]:
    """Split SQL into significant tokens; whitespace and comments set ``spaced``."""
    tokens: list[_Tok] = []
    spaced = False
    for match in _TOKEN_RE.finditer(sql):
        kind = match.lastgroup or "op"
        text = match.group()
        if kind in ("ws", "comment"):
            spaced = True
            continue
        if kind == "open_comment":
            raise HdxSanitizeError("The SQL has a /* comment that is never closed.")
        if kind == "op" and text in ("'", '"', "`"):
            raise HdxSanitizeError(f"The SQL has an unterminated {text} quote.")
        tokens.append(_Tok(kind, text, spaced))
        spaced = False
    return tokens


def _render(toks: Sequence[_Tok]) -> str:
    """Re-emit tokens with single spaces where the input had whitespace."""
    parts: list[str] = []
    for i, tok in enumerate(toks):
        if (
            i
            and tok.spaced
            and toks[i - 1].text not in _OPEN
            and tok.text not in _CLOSE
            and tok.text != ","
        ):
            parts.append(" ")
        parts.append(tok.text)
    return "".join(parts)


def _snippet(toks: Sequence[_Tok]) -> str:
    text = _render(toks)
    return text if len(text) <= 120 else f"{text[:117]}..."


def _walk(toks: Sequence[_Tok]) -> list[int]:
    """Nesting depth at each token, counting brackets and open CASE ... END blocks."""
    depths: list[int] = []
    brackets = 0
    cases = 0
    for i, tok in enumerate(toks):
        dotted = i > 0 and toks[i - 1].text == "."
        if tok.text in _CLOSE:
            brackets -= 1
        elif tok.keyword == "END" and cases and not dotted:
            cases -= 1
        depths.append(brackets + cases)
        if tok.text in _OPEN:
            brackets += 1
        elif tok.keyword == "CASE" and not dotted:
            cases += 1
    return depths


def _split(toks: Sequence[_Tok], is_separator: Callable[[_Tok], bool]) -> list[list[_Tok]]:
    parts: list[list[_Tok]] = [[]]
    for tok, depth in zip(toks, _walk(toks), strict=True):
        if depth == 0 and is_separator(tok):
            parts.append([])
        else:
            parts[-1].append(tok)
    return parts


def _split_commas(toks: Sequence[_Tok]) -> list[list[_Tok]]:
    return _split(toks, lambda tok: tok.text == ",")


def _split_and(toks: Sequence[_Tok]) -> list[list[_Tok]]:
    """Split on top-level AND, leaving the AND of ``x BETWEEN a AND b`` inside."""
    parts: list[list[_Tok]] = [[]]
    open_betweens = 0
    for tok, depth in zip(toks, _walk(toks), strict=True):
        if depth == 0 and tok.keyword == "BETWEEN":
            open_betweens += 1
        if depth == 0 and tok.keyword == "AND":
            if open_betweens:
                open_betweens -= 1
            else:
                parts.append([])
                continue
        parts[-1].append(tok)
    return parts


def _has_top_level(toks: Sequence[_Tok], texts: frozenset[str]) -> bool:
    for tok, depth in zip(toks, _walk(toks), strict=True):
        if depth == 0 and (tok.keyword in texts or (tok.kind == "op" and tok.text in texts)):
            return True
    return False


def _matching_close(toks: Sequence[_Tok], start: int) -> int:
    depth = 0
    for i in range(start, len(toks)):
        if toks[i].text in _OPEN:
            depth += 1
        elif toks[i].text in _CLOSE:
            depth -= 1
            if depth == 0:
                return i
    raise HdxSanitizeError("The SQL has an unclosed bracket.")


def _unwrap(toks: Sequence[_Tok]) -> list[_Tok] | None:
    """Return the inside of ``( ... )`` when one bracket pair spans every token."""
    if len(toks) >= 2 and toks[0].text == "(" and _matching_close(toks, 0) == len(toks) - 1:
        return list(toks[1:-1])
    return None


def _unwrap_all(toks: Sequence[_Tok]) -> list[_Tok]:
    inner = list(toks)
    while (unwrapped := _unwrap(inner)) is not None:
        inner = unwrapped
    return inner


def _name_of(tok: _Tok) -> str | None:
    if tok.kind == "word":
        return tok.text
    if tok.kind == "ident" and tok.text[0] == "`":
        return tok.text[1:-1].replace("``", "`")
    if tok.kind == "ident":
        return tok.text[1:-1].replace('""', '"')
    return None


def _has_epoch(toks: Sequence[_Tok]) -> bool:
    return any(tok.kind == "word" and _EPOCH_RE.match(tok.text) for tok in toks)


def _hdx_names(toks: Sequence[_Tok]) -> list[str]:
    names = (_name_of(tok) for tok in toks)
    return [name for name in names if name and _HDX_NAME_RE.match(name)]


def _is_aggregate_name(name: str) -> bool:
    stem = name.lower()
    while stem not in _AGGREGATES:
        suffix = next((c for c in _COMBINATORS if stem.endswith(c) and stem != c), None)
        if suffix is None:
            return False
        stem = stem[: -len(suffix)]
    return True


def _is_aggregate(toks: Sequence[_Tok]) -> bool:
    for i, tok in enumerate(toks):
        if tok.keyword == "OVER":
            return True
        called = i + 1 < len(toks) and toks[i + 1].text == "("
        if tok.kind == "word" and called and _is_aggregate_name(tok.text):
            return True
    return False


def _split_alias(item: Sequence[_Tok]) -> tuple[list[_Tok], str | None]:
    """Split ``expr AS name`` into the expression and the alias name."""
    if len(item) >= 3 and item[-2].keyword == "AS" and _name_of(item[-1]) is not None:
        return list(item[:-2]), _name_of(item[-1])
    return list(item), None


def _is_interval(toks: Sequence[_Tok]) -> bool:
    return len(toks) == 3 and toks[0].keyword == "INTERVAL" and toks[2].kind == "word"


def _is_bound(toks: Sequence[_Tok]) -> bool:
    """True for an epoch instant, optionally wrapped in ``to*()`` and shifted by an INTERVAL."""
    inner = _unwrap_all(toks)
    if len(inner) >= 5 and inner[-4].text in ("+", "-") and _is_interval(inner[-3:]):
        return _is_bound(inner[:-4])
    if len(inner) < 4 or inner[0].kind != "word" or inner[1].text != "(":
        return False
    if _matching_close(inner, 1) != len(inner) - 1:
        return False
    args = _split_commas(inner[2:-1])
    if _EPOCH_RE.match(inner[0].text):
        return len(args) == 1 and len(args[0]) == 1 and args[0][0].kind == "number"
    if not _TO_FUNCTION_RE.match(inner[0].text) or not _is_bound(args[0]):
        return False
    return all(
        _is_interval(arg) or (len(arg) == 1 and arg[0].kind in ("number", "string"))
        for arg in args[1:]
    )


def _is_column(toks: Sequence[_Tok]) -> bool:
    """True for a column or a function of one, never a negated or compound predicate."""
    return (
        bool(toks)
        and not _has_epoch(toks)
        and all(tok.kind != "string" and tok.keyword not in _PREDICATE_WORDS for tok in toks)
    )


def _is_time_bound(toks: Sequence[_Tok]) -> bool:
    """True for ``col >= <instant>``, its mirror, or ``col BETWEEN <instant> AND <instant>``."""
    depths = _walk(toks)
    ops = [i for i, tok in enumerate(toks) if depths[i] == 0 and tok.text in _COMPARISON_OPS]
    betweens = [i for i, tok in enumerate(toks) if depths[i] == 0 and tok.keyword == "BETWEEN"]
    if len(ops) == 1 and not betweens:
        at = ops[0]
        left, right = toks[:at], toks[at + 1 :]
        if toks[at].text not in _WINDOW_OPS:
            return False
        return (_is_column(left) and _is_bound(right)) or (_is_column(right) and _is_bound(left))
    if len(betweens) == 1 and not ops:
        at = betweens[0]
        ands = [i for i in range(at + 1, len(toks)) if depths[i] == 0 and toks[i].keyword == "AND"]
        if len(ands) != 1:
            return False
        low, high = toks[at + 1 : ands[0]], toks[ands[0] + 1 :]
        return _is_column(toks[:at]) and _is_bound(low) and _is_bound(high)
    return False


class HdxSanitizer:
    """Reduce rendered HyperDX SQL to the SQL a detection rule is built from."""

    def sanitize(self, sql: str) -> HdxSanitizeResult:
        """Strip the view's chrome, keeping the source table and every user predicate.

        Args:
            sql: SQL rendered by HyperDX for the view the user is looking at.

        Returns:
            The rule base and what was stripped to reach it. Empty input gives an
            empty ``clean_sql``.

        Raises:
            HdxSanitizeError: If the SQL holds an unrendered placeholder, or a
                construct the rule cannot carry without changing what it matches.
        """
        result = HdxSanitizeResult()
        toks = _tokenize(sql)
        if not toks:
            return result

        for i, tok in enumerate(toks):
            if tok.text in ("{", "}"):
                end = i
                while end + 1 < len(toks) and (toks[end].text != "}" or toks[end + 1].text == "}"):
                    end += 1
                raise HdxSanitizeError(
                    f"The SQL still holds the unrendered template placeholder "
                    f"{_snippet(toks[i : end + 1])}. Send the SQL HyperDX ran for the view, "
                    "not the export template."
                )
            if tok.kind == "macro":
                raise HdxSanitizeError(
                    f"The SQL still holds the unrendered HyperDX macro {tok.text}. "
                    "Send the SQL HyperDX ran for the view, not the SQL template."
                )

        while toks and toks[-1].text == ";":
            toks.pop()
        _check_brackets(toks)
        if any(tok.text == ";" for tok, depth in zip(toks, _walk(toks), strict=True) if not depth):
            raise HdxSanitizeError("The SQL holds more than one statement.")

        try:
            reduced = self._reduce(_unwrap_all(toks), result)
        except RecursionError:
            raise HdxSanitizeError("The SQL is nested too deeply to reduce safely.") from None
        clean = f"SELECT {', '.join(reduced.projection)} FROM {reduced.table}"
        if reduced.where:
            clean = f"{clean} WHERE {reduced.where}"
        else:
            result.warnings.append(
                f"No filter remains once the view's time window is removed, so a rule "
                f"built from this SQL matches every row of {reduced.table}."
            )
        result.clean_sql = clean
        return result

    def _reduce(self, toks: list[_Tok], result: HdxSanitizeResult) -> _Reduced:
        clauses = _clauses(toks)
        by_name = {clause.name: clause for clause in clauses}
        if "FROM" not in by_name:
            raise HdxSanitizeError("The SQL has no FROM clause, so there is no table to scan.")

        aliases: dict[str, list[_Tok]] = {}
        if "WITH" in by_name:
            aliases = self._with_aliases(by_name["WITH"], result)

        source = by_name["FROM"].body
        if source and source[0].text == "(":
            return self._unwrap_subquery(clauses, result)
        table = _table(source, result)

        items = _split_commas(by_name["SELECT"].body)
        if not all(items):
            raise HdxSanitizeError("The SELECT list has an empty column.")
        projection = self._projection(items, aliases, result)
        for item in items:
            expr, alias = _split_alias(item)
            if alias and alias not in aliases and not _is_aggregate(expr) and not _hdx_names(item):
                aliases[alias] = _substitute(expr, aliases)

        pruned = [
            self._prune(_substitute(by_name[name].body, aliases), result)
            for name in ("PREWHERE", "WHERE")
            if name in by_name
        ]
        kept = [part for part in pruned if part is not None]
        if len(kept) > 1:
            where = " AND ".join(f"({p.text})" if p.bare_or else p.text for p in kept)
        else:
            where = kept[0].text if kept else None

        self._record_tail(clauses, result)
        return _Reduced(projection=projection, table=table, where=where)

    def _unwrap_subquery(self, clauses: list[_Clause], result: HdxSanitizeResult) -> _Reduced:
        """Reduce ``SELECT ... FROM (<query>)`` to the inner query when the outer filters nothing."""
        by_name = {clause.name: clause for clause in clauses}
        for name in ("PREWHERE", "WHERE"):
            if name in by_name:
                raise HdxSanitizeError(
                    f"The query filters a derived table ({name} "
                    f"{_snippet(by_name[name].body)}); a rule filters a source table."
                )
        source = by_name["FROM"].body
        close = _matching_close(source, 0)
        result.stripped_clauses.append(f"SELECT {_snippet(by_name['SELECT'].body)} FROM (...)")
        self._record_tail(clauses, result)
        return self._reduce(_unwrap_all(source[: close + 1]), result)

    def _with_aliases(self, clause: _Clause, result: HdxSanitizeResult) -> dict[str, list[_Tok]]:
        aliases: dict[str, list[_Tok]] = {}
        for item in _split_commas(clause.body):
            name = _name_of(item[0]) if item else None
            if len(item) >= 4 and item[1].keyword == "AS" and item[3].keyword in ("SELECT", "WITH"):
                if name == _SERIES_CAP:
                    result.stripped_clauses.append(f"WITH {_snippet(item)}")
                    continue
                if name in _METRIC_CTES:
                    raise HdxSanitizeError(
                        "This is a metric chart. Rules are built from log and trace searches."
                    )
                raise HdxSanitizeError(
                    f"The query defines the CTE {name}; a rule reads one source table directly."
                )
            expr, alias = _split_alias(item)
            if alias is None or not expr:
                raise HdxSanitizeError(f"The WITH item {_snippet(item)} is not a named expression.")
            aliases[alias] = _substitute(expr, aliases)
            result.stripped_clauses.append(f"WITH {_snippet(item)}")
        return aliases

    @staticmethod
    def _projection(
        items: list[list[_Tok]],
        aliases: dict[str, list[_Tok]],
        result: HdxSanitizeResult,
    ) -> list[str]:
        """Keep row-level columns; drop aggregates and time buckets, falling back to ``*``."""
        kept: list[str] = []
        for item in items:
            expr, _ = _split_alias(item)
            if _hdx_names(item):
                result.stripped_time_bucket_select.append(_render(item))
                result.had_time_bucket = True
            elif _is_aggregate(expr):
                result.stripped_clauses.append(f"SELECT {_render(item)}")
            else:
                kept.append(_render(_substitute(item, aliases)))
        return kept or ["*"]

    def _prune(self, toks: list[_Tok], result: HdxSanitizeResult) -> _Pruned | None:
        """Drop time-window and series-cap conjuncts, keeping every other predicate."""
        if not toks:
            raise HdxSanitizeError("The SQL has an empty filter clause.")
        if _has_top_level(toks, frozenset({"OR", "?", "->"})):
            _check_leaf(toks)
            return _Pruned(_render(toks), bare_or=True)
        parts = _split_and(toks)
        if len(parts) > 1:
            kept = [p for p in (self._prune(part, result) for part in parts) if p is not None]
            return _Pruned(" AND ".join(p.text for p in kept), bare_or=False) if kept else None
        inner = _unwrap(toks)
        if inner is not None:
            pruned = self._prune(inner, result)
            return None if pruned is None else _Pruned(f"({pruned.text})", bare_or=False)
        if _is_time_bound(toks):
            result.stripped_time_bounds.append(_render(toks))
            return None
        if _SERIES_CAP in _hdx_names(toks):
            result.stripped_clauses.append(f"WHERE {_render(toks)}")
            return None
        _check_leaf(toks)
        return _Pruned(_render(toks), bare_or=False)

    @staticmethod
    def _record_tail(clauses: list[_Clause], result: HdxSanitizeResult) -> None:
        """Record the display clauses after WHERE, which the rule base drops."""
        limits: list[str] = []
        for clause in clauses:
            text = _render([*clause.head, *clause.body])
            if clause.name in ("GROUP BY", "ORDER BY"):
                for item in _split_commas(clause.body):
                    if _hdx_names(item):
                        result.stripped_time_bucket_refs.append(_render(item))
                        result.had_time_bucket = True
                result.stripped_clauses.append(text)
            elif clause.name in ("LIMIT", "OFFSET", "FETCH"):
                limits.append(text)
            elif clause.name == "SETTINGS":
                result.stripped_settings = text
            elif clause.name in ("INTO OUTFILE", "FORMAT"):
                result.stripped_clauses.append(text)
        if limits:
            result.stripped_limit = " ".join(filter(None, [result.stripped_limit, *limits]))


def _check_brackets(toks: Sequence[_Tok]) -> None:
    depth = 0
    for tok in toks:
        if tok.text in _OPEN:
            depth += 1
        elif tok.text in _CLOSE:
            depth -= 1
            if depth < 0:
                raise HdxSanitizeError("The SQL has a closing bracket with no opening one.")
    if depth:
        raise HdxSanitizeError("The SQL has an unclosed bracket.")


def _check_leaf(toks: Sequence[_Tok]) -> None:
    """Refuse a kept predicate that still carries view chrome."""
    if _has_epoch(toks):
        raise HdxSanitizeError(
            f"A time bound sits inside an OR, a NOT or a function call "
            f"({_snippet(toks)}), so removing it would change what the rule matches."
        )
    if names := _hdx_names(toks):
        raise HdxSanitizeError(
            f"The filter references the HyperDX display column {names[0]} "
            f"({_snippet(toks)}), which a rule cannot read."
        )


def _clause_name(toks: Sequence[_Tok], i: int) -> tuple[str, int] | None:
    """Name the clause opened at ``toks[i]`` and how many keyword tokens open it."""
    tok = toks[i]
    if tok.kind != "word" or (i and toks[i - 1].text == "."):
        return None
    word = tok.keyword
    after = toks[i + 1] if i + 1 < len(toks) else None
    beyond = toks[i + 2] if i + 2 < len(toks) else None
    following = after.keyword if after else ""
    if word in ("GROUP", "ORDER") and following == "BY":
        return f"{word} BY", 2
    if word == "INTO" and following == "OUTFILE":
        return "INTO OUTFILE", 2
    if word not in _SINGLE_WORD_CLAUSES:
        return None
    # These words double as column names, so each needs the shape its clause takes.
    if word == "WITH" and following in _WITH_MODIFIERS:
        return None
    if word == "EXCEPT" and i and toks[i - 1].text == "*":
        return None
    if word in ("LIMIT", "OFFSET") and not (
        after is not None and (after.kind == "number" or after.text == "(")
    ):
        return None
    if word == "FETCH" and following not in ("FIRST", "NEXT"):
        return None
    if word == "FORMAT":
        ends = beyond is None or _clause_name(toks, i + 2) is not None
        return (word, 1) if after is not None and after.kind == "word" and ends else None
    if word == "SETTINGS" and (beyond is None or beyond.text != "="):
        return None
    if word == "WINDOW" and (beyond is None or beyond.keyword != "AS"):
        return None
    return word, 1


def _clauses(toks: list[_Tok]) -> list[_Clause]:
    """Split one SELECT statement into its top-level clauses, in order."""
    clauses: list[_Clause] = []
    depths = _walk(toks)
    i = 0
    while i < len(toks):
        opened = _clause_name(toks, i) if depths[i] == 0 else None
        if opened is None:
            if not clauses:
                raise HdxSanitizeError(f"The SQL is not a SELECT (it starts {_snippet(toks[:4])}).")
            clauses[-1].body.append(toks[i])
            i += 1
            continue
        name, width = opened
        clauses.append(_Clause(name, list(toks[i : i + width]), []))
        i += width

    if not clauses or clauses[0].name not in ("WITH", "SELECT"):
        raise HdxSanitizeError(f"The SQL is not a SELECT (it starts {_snippet(toks[:4])}).")
    rank = -1
    seen: set[str] = set()
    for clause in clauses:
        if clause.name in _REFUSED_CLAUSES:
            raise HdxSanitizeError(_REFUSED_CLAUSES[clause.name])
        if clause.name in seen and clause.name != "LIMIT":
            raise HdxSanitizeError(f"The SQL has two top-level {clause.name} clauses.")
        if _CLAUSE_RANK[clause.name] < rank:
            raise HdxSanitizeError(f"The {clause.name} clause is out of order.")
        rank = _CLAUSE_RANK[clause.name]
        seen.add(clause.name)
    if "SELECT" not in seen:
        raise HdxSanitizeError("The SQL has no SELECT list.")
    return clauses


def _table(source: list[_Tok], result: HdxSanitizeResult) -> str:
    """Validate the FROM target: one table, optionally with FINAL, SAMPLE or an alias."""
    if not source:
        raise HdxSanitizeError("The FROM clause names no table.")
    if len(_split_commas(source)) > 1:
        raise HdxSanitizeError("The FROM clause lists several tables; a rule reads one.")
    if len(source) > 1 and source[1].text == "(":
        raise HdxSanitizeError(
            f"The FROM clause reads the table function {source[0].text}(); "
            "a rule reads a source table."
        )
    keywords = {tok.keyword for tok in source}
    for keyword in ("FINAL", "SAMPLE"):
        if keyword in keywords:
            result.warnings.append(
                f"The table reference keeps {keyword}, which the hunt runner does not "
                "apply when it scans the table."
            )
    return _render(source)


def _substitute(toks: Sequence[_Tok], aliases: dict[str, list[_Tok]]) -> list[_Tok]:
    """Replace references to SELECT and WITH aliases with the aliased expression.

    A rule runs without the view's SELECT list, so an alias the filter leaned on
    has to become the expression it named. Subqueries keep their own scope.
    """
    if not aliases:
        return list(toks)
    out: list[_Tok] = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        after = toks[i + 1] if i + 1 < len(toks) else None
        if tok.text == "(" and after is not None and after.keyword in ("SELECT", "WITH"):
            close = _matching_close(toks, i)
            out.extend(toks[i : close + 1])
            i = close + 1
            continue
        if (lambda_end := _lambda_end(toks, i)) is not None:
            arrow = next(j for j in range(i, lambda_end) if toks[j].text == "->")
            params = {_name_of(t) for t in toks[i:arrow]}
            scoped = {name: expr for name, expr in aliases.items() if name not in params}
            out.extend(toks[i : arrow + 1])
            out.extend(_substitute(toks[arrow + 1 : lambda_end], scoped))
            i = lambda_end
            continue
        name = _name_of(tok)
        replacement = aliases.get(name) if name else None
        dotted = i > 0 and toks[i - 1].text == "."
        called = after is not None and after.text in ("(", ".")
        if replacement is None or dotted or called:
            out.append(tok)
        else:
            inner = _unwrap_all(replacement)
            if len(inner) > 1:
                inner = [_Tok("op", "(", False), *inner, _Tok("op", ")", False)]
            out.append(_Tok(inner[0].kind, inner[0].text, tok.spaced))
            out.extend(inner[1:])
        i += 1
    return out


def _lambda_end(toks: Sequence[_Tok], start: int) -> int | None:
    """End index (exclusive) of a lambda whose parameters start at ``toks[start]``."""
    if toks[start].text == "(":
        close = _matching_close(toks, start)
        arrow = close + 1
    elif _name_of(toks[start]) is not None:
        arrow = start + 1
    else:
        return None
    if arrow >= len(toks) or toks[arrow].text != "->":
        return None
    depth = 0
    for j in range(arrow + 1, len(toks)):
        if toks[j].text in _OPEN:
            depth += 1
        elif toks[j].text in _CLOSE:
            if depth == 0:
                return j
            depth -= 1
        elif toks[j].text == "," and depth == 0:
            return j
    return len(toks)
