"""Tests for query fingerprinting."""

import pytest

from dfe_engine.hunts.fingerprint import fingerprint_query, normalize_query


class TestNormalizeQuery:
    """Test SQL normalization for fingerprinting."""

    def test_collapse_whitespace(self):
        sql = "SELECT   *   FROM   t   WHERE   x = 1"
        assert "  " not in normalize_query(sql)

    def test_lowercase(self):
        assert normalize_query("SELECT * FROM T") == normalize_query("select * from t")

    def test_strip_single_line_comment(self):
        sql = "SELECT * FROM t -- this is a comment\nWHERE x = 1"
        result = normalize_query(sql)
        assert "comment" not in result
        assert "where" in result

    def test_strip_multi_line_comment(self):
        sql = "SELECT /* all columns */ * FROM t"
        result = normalize_query(sql)
        assert "all columns" not in result
        assert "select" in result

    def test_replace_string_literals(self):
        result = normalize_query("SELECT * FROM t WHERE name = 'alice'")
        assert "alice" not in result
        assert "'?'" in result

    def test_replace_integer_literals(self):
        result = normalize_query("SELECT * FROM t WHERE id = 42")
        assert "42" not in result

    def test_replace_float_literals(self):
        result = normalize_query("SELECT * FROM t WHERE score > 3.14")
        assert "3.14" not in result

    def test_strip_trailing_semicolons(self):
        result = normalize_query("SELECT 1;")
        assert not result.endswith(";")

    def test_empty_string(self):
        assert normalize_query("") == ""

    def test_different_literals_same_structure(self):
        """Queries with same structure but different values should normalize identically."""
        q1 = "SELECT * FROM t WHERE id = 1 AND name = 'alice'"
        q2 = "SELECT * FROM t WHERE id = 999 AND name = 'bob'"
        assert normalize_query(q1) == normalize_query(q2)

    def test_different_structure_differs(self):
        q1 = "SELECT * FROM t WHERE id = 1"
        q2 = "SELECT * FROM t WHERE name = 'alice'"
        assert normalize_query(q1) != normalize_query(q2)

    def test_preserves_identifiers(self):
        """Table and column names should be preserved."""
        result = normalize_query("SELECT col_a, col_b FROM my_table")
        assert "col_a" in result
        assert "col_b" in result
        assert "my_table" in result


class TestFingerprintQuery:
    """Test fingerprint generation."""

    def test_returns_hex_string(self):
        fp = fingerprint_query("SELECT 1")
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)

    def test_stable_across_calls(self):
        sql = "SELECT * FROM t WHERE id = 42"
        assert fingerprint_query(sql) == fingerprint_query(sql)

    def test_same_structure_same_fingerprint(self):
        q1 = "SELECT * FROM events WHERE ts > '2025-01-01' AND org_id = 'abc'"
        q2 = "SELECT * FROM events WHERE ts > '2026-03-03' AND org_id = 'xyz'"
        assert fingerprint_query(q1) == fingerprint_query(q2)

    def test_different_structure_different_fingerprint(self):
        q1 = "SELECT * FROM events WHERE ts > '2025-01-01'"
        q2 = "INSERT INTO results SELECT * FROM events"
        assert fingerprint_query(q1) != fingerprint_query(q2)

    def test_whitespace_insensitive(self):
        q1 = "SELECT  *  FROM  t"
        q2 = "SELECT * FROM t"
        assert fingerprint_query(q1) == fingerprint_query(q2)

    def test_case_insensitive(self):
        q1 = "SELECT * FROM Events WHERE Id = 1"
        q2 = "select * from events where id = 1"
        assert fingerprint_query(q1) == fingerprint_query(q2)

    def test_real_hunt_query(self):
        """Realistic hunt query with timestamp condition."""
        q1 = (
            "INSERT INTO dfe_audit.hunt_results "
            "SELECT * FROM default.windows_audit "
            "WHERE (timestamp_load >= '2026-03-01 00:00:00' "
            "AND timestamp_load < '2026-03-02 00:00:00') "
            "AND EventID = 4688"
        )
        q2 = (
            "INSERT INTO dfe_audit.hunt_results "
            "SELECT * FROM default.windows_audit "
            "WHERE (timestamp_load >= '2026-03-03 12:00:00' "
            "AND timestamp_load < '2026-03-03 12:05:00') "
            "AND EventID = 4688"
        )
        assert fingerprint_query(q1) == fingerprint_query(q2)
