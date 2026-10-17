#  Project:      dfe-engine
#  File:         tests/unit/test_integration_harness.py
#  Purpose:      The integration harness fails in CI where it would skip locally
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The integration fixtures report a missing service as a skip locally and a failure in CI."""

import pytest

from tests.integration.conftest import _unavailable


@pytest.fixture
def locally(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run outside CI."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def test_a_missing_service_skips_outside_ci(locally: None):
    with pytest.raises(pytest.skip.Exception, match="ClickHouse not reachable"):
        _unavailable("ClickHouse not reachable: refused")


@pytest.mark.parametrize(
    ("name", "value"),
    [("CI", "true"), ("CI", "1"), ("GITHUB_ACTIONS", "true")],
    ids=["ci-true", "ci-1", "github-actions"],
)
def test_a_missing_service_fails_in_ci(locally: None, monkeypatch, name: str, value: str):
    monkeypatch.setenv(name, value)

    with pytest.raises(pytest.fail.Exception, match="ClickHouse not reachable"):
        _unavailable("ClickHouse not reachable: refused")
