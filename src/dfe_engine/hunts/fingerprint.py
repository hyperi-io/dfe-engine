"""Query fingerprinting — normalise SQL text for grouping execution stats across runs.

Produces a short, stable hash that groups semantically identical queries
(same structure, different literal values) into a single fingerprint.

Usage:
    from dfe_engine.hunts.fingerprint import fingerprint_query, normalize_query

    fp = fingerprint_query("SELECT * FROM t WHERE id = 42")
    # -> "a1b2c3d4e5f67890"
"""

import hashlib
import re


def fingerprint_query(sql: str) -> str:
    """Return a 16-char hex fingerprint of the normalised SQL."""
    normalized = normalize_query(sql)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def normalize_query(sql: str) -> str:
    """Strip comments, collapse whitespace, replace literals with placeholders.

    The result is a canonical form suitable for hashing — two queries that
    differ only in literal values will produce the same normalised string.
    """
    # Remove single-line comments
    sql = re.sub(r"--[^\n]*", "", sql)
    # Remove multi-line comments
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    # Replace string literals with placeholder
    sql = re.sub(r"'[^']*'", "'?'", sql)
    # Replace numeric literals (integers and decimals) with placeholder
    # Use word boundaries to avoid replacing digits inside identifiers
    sql = re.sub(r"\b\d+\.?\d*\b", "?", sql)
    # Collapse whitespace
    sql = re.sub(r"\s+", " ", sql).strip()
    # Lowercase
    sql = sql.lower()
    # Strip trailing semicolons
    sql = sql.rstrip(";").strip()
    return sql
