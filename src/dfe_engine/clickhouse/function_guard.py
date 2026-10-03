#  Project:      dfe-engine
#  File:         clickhouse/function_guard.py
#  Purpose:      Refuse caller SQL that calls a ClickHouse function reading outside the row
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""THE one list of ClickHouse calls caller-supplied SQL may not make.

A caller writes SQL in three places: a sampler filter, a detection rule's
condition, and a schema column's default expression. Each is spliced into a
statement the engine runs against ClickHouse as a user that may read anything,
so a call such as ``url()`` turns a filter into an outbound HTTP request
carrying the row, and ``s3()`` or an ``ai*`` call does the same to another
service. ClickHouse's ``readonly=1`` refuses ``url()`` but not an ``ai*`` call,
and a statement that must write cannot set it at all, so the refusal has to
happen before the SQL is sent.

Three families, each its own refusal:

* a table function, a dictionary reader, or a scalar that reads a Join table or
  another table's schema -- it reads outside the row;
* the function forms of ``IN`` (``globalIn``, ``notIn`` and the rest), each of
  which resolves a name in its second argument as a table;
* a call that posts its arguments to another service (``ai*``) or writes a
  counter to Keeper (``generateSerialID``).

The check reads the text that will run: every name directly followed by ``(``
is looked up, so a quoted call name is caught as well as a bare one.
"""

import itertools
import re

from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import SqlglotError
from sqlglot.tokens import Token, TokenType

_CLICKHOUSE = Dialect.get_or_raise("clickhouse")


class CallNotPermittedError(ValueError):
    """Caller SQL calls a function that reads outside the row or calls out."""


# ClickHouse 26.9.4 table functions, bar format and fuzzQuery, whose scalars read only their args.
TABLE_FUNCTIONS = frozenset(
    {
        "arrowflight",
        "azureblobstorage",
        "azureblobstoragecluster",
        "bigquery",
        "cluster",
        "clusterallreplicas",
        "cosn",
        "deltalake",
        "deltalakeazure",
        "deltalakeazurecluster",
        "deltalakecluster",
        "deltalakelocal",
        "deltalakes3",
        "deltalakes3cluster",
        "dictionary",
        "eval",
        "executable",
        "file",
        "filecluster",
        "filesystem",
        "fuzzjson",
        "gcs",
        "generate_series",
        "generaterandom",
        "generateseries",
        "hdfs",
        "hdfscluster",
        "hive",
        "hudi",
        "hudicluster",
        "iceberg",
        "icebergazure",
        "icebergazurecluster",
        "icebergcluster",
        "iceberghdfs",
        "iceberghdfscluster",
        "iceberglocal",
        "iceberglocalcluster",
        "icebergs3",
        "icebergs3cluster",
        "input",
        "jdbc",
        "loop",
        "merge",
        "mergetreeanalyzeindexes",
        "mergetreeanalyzeindexesuuid",
        "mergetreecodecblockcounts",
        "mergetreeindex",
        "mergetreeprojection",
        "mergetreetextindex",
        "mongodb",
        "mysql",
        "null",
        "numbers",
        "numbers_mt",
        "odbc",
        "oss",
        "paimon",
        "paimonazure",
        "paimonazurecluster",
        "paimoncluster",
        "paimonhdfs",
        "paimonhdfscluster",
        "paimonlocal",
        "paimons3",
        "paimons3cluster",
        "postgresql",
        "primes",
        "prometheusquery",
        "prometheusqueryrange",
        "redis",
        "remote",
        "remotesecure",
        "s3",
        "s3cluster",
        "sqlite",
        "sqlstandardvalues",
        "timeseriesdata",
        "timeseriesmetricfamilies",
        "timeseriesmetrics",
        "timeseriessamples",
        "timeseriesselector",
        "timeseriestags",
        "url",
        "urlcluster",
        "values",
        "view",
        "viewexplain",
        "viewifpermitted",
        "ytsaurus",
        "zeros",
        "zeros_mt",
    }
)

# Scalars that read a Join table, another table's schema, or a dictionary by a model name.
OUTSIDE_READERS = frozenset(
    {
        "hascolumnintable",
        "joinget",
        "joingetornull",
        "naivebayesclassifier",
        "naivebayesclassifierwithallprobs",
        "naivebayesclassifierwithprob",
    }
)
# dict* read external dictionaries and region* the embedded ones.
DICTIONARY_READER_PREFIXES = ("dict", "region")

# generateSerialID writes a counter named by its argument to Keeper.
REMOTE_CALLERS = frozenset({"generateserialid"})
# ai* post their arguments to an LLM provider, and readonly=1 does not stop them.
REMOTE_CALLER_PREFIXES = ("ai",)

# The function forms of IN, each of which resolves a name in its second argument as a table.
IN_FUNCTION = re.compile(r"(global)?(not)?(null)?in(ignoreset)?", re.IGNORECASE)


def reads_outside_the_row(function_name: str) -> bool:
    """Whether a call by this name reads a table, file, dictionary or Join table."""
    name = function_name.lower()
    return (
        name in TABLE_FUNCTIONS
        or name in OUTSIDE_READERS
        or name.startswith(DICTIONARY_READER_PREFIXES)
    )


def calls_another_service(function_name: str) -> bool:
    """Whether a call by this name sends its arguments to an AI provider or Keeper."""
    name = function_name.lower()
    return name in REMOTE_CALLERS or name.startswith(REMOTE_CALLER_PREFIXES)


def tokenize(sql: str, *, subject: str) -> list[Token]:
    """Tokenize ``sql`` as ClickHouse, turning a failure into a refusal.

    Args:
        sql: The caller's SQL.
        subject: What the caller supplied, for the message ("filter", ...).

    Returns:
        The tokens.

    Raises:
        CallNotPermittedError: If ClickHouse's tokens cannot be read, which
            leaves the calls in the SQL unknown.
    """
    try:
        return _CLICKHOUSE.tokenize(sql)
    except SqlglotError as exc:
        detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise CallNotPermittedError(f"{subject} is not ClickHouse SQL: {detail}") from exc
    # sqlglot raises AttributeError and RecursionError on some input it cannot read.
    except Exception as exc:
        raise CallNotPermittedError(f"{subject} is not ClickHouse SQL") from exc


def refuse_calls_outside_the_row(
    sql: str, *, subject: str, tokens: list[Token] | None = None
) -> None:
    """Raise for the first call in ``sql`` that reads outside the row or calls out.

    Args:
        sql: The SQL that will run, not a form it was rewritten from.
        subject: What the caller supplied, named in the message ("filter",
            "a rule's detection condition", ...).
        tokens: ``sql``'s tokens where the caller already has them.

    Raises:
        CallNotPermittedError: Naming the call and which refusal it fell to.
    """
    for name, following in itertools.pairwise(
        tokens if tokens is not None else tokenize(sql, subject=subject)
    ):
        # The IN operator's own keyword is followed by its list, not a call.
        if following.token_type is not TokenType.L_PAREN or name.token_type is TokenType.IN:
            continue
        if IN_FUNCTION.fullmatch(name.text):
            raise CallNotPermittedError(
                f"{subject} may not call {name.text}(), which can read a table: "
                "use the IN operator with a list of values"
            )
        if reads_outside_the_row(name.text):
            raise CallNotPermittedError(
                f"{subject} may not call {name.text}(), which reads outside the table"
            )
        if calls_another_service(name.text):
            raise CallNotPermittedError(
                f"{subject} may not call {name.text}(), which sends data to another service"
            )
