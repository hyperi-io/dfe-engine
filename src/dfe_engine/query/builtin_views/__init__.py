"""Builtin parameterized view SQL definitions.

Each .sql file defines a CREATE OR REPLACE VIEW statement following the
naming convention: dfe_v_{namespace}_{name}.sql

These are applied to ClickHouse by DDLManager.bootstrap() or
DDLManager.apply_all_builtin_views().
"""
