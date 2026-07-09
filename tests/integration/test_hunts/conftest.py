"""Shared fixtures for hunt integration tests."""

import uuid

import pytest


@pytest.fixture
def unique_names():
    """A unique (database, table) pair per test so runs never collide and each
    test creates + drops its own throwaway audit table on the shared cluster."""
    unique_id = uuid.uuid4().hex
    return f"dfe_audit_{unique_id}", f"detection_checkpoint_{unique_id}"
