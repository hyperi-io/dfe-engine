#  Project:      dfe-engine
#  File:         tests/unit/test_integration_harness.py
#  Purpose:      The integration harness fails in CI where it would skip locally
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The integration fixtures report a missing service as a skip locally and a failure in CI.

They also pick the ClickHouse a run reaches. The app settings read a checkout's .env,
which can name a shared server, so they count only when the cluster tier is asked for.
``DFE_CLICKHOUSE_HOST`` stands in for that .env here.
"""

import pytest

from tests.integration.conftest import _cluster_params, _resolve_tier, _unavailable

APP_HOST = "shared-ch.example.internal"
TEST_HOST = "test-ch.example.internal"


@pytest.fixture
def app_names_a_clickhouse(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app settings name a non-local ClickHouse, and no test target or tier is set."""
    monkeypatch.setenv("DFE_CLICKHOUSE_HOST", APP_HOST)
    for name in ("DFE_TEST_CH_HOST", "DFE_TEST_TIER", "DFE_TEST_DOCKER_HOST"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("tier", ["", "cluster"], ids=["auto", "cluster"])
def test_the_test_target_wins_over_the_app_settings(app_names_a_clickhouse, monkeypatch, tier):
    monkeypatch.setenv("DFE_TEST_CH_HOST", TEST_HOST)
    monkeypatch.setenv("DFE_TEST_TIER", tier)

    params = _cluster_params()

    assert params is not None
    assert params["host"] == TEST_HOST
    assert _resolve_tier() == "cluster"


def test_a_run_never_takes_the_app_settings_host_unasked(app_names_a_clickhouse):
    assert _cluster_params() is None
    assert _resolve_tier() == "local"


def test_the_app_settings_host_counts_when_the_cluster_tier_is_asked_for(
    app_names_a_clickhouse, monkeypatch
):
    monkeypatch.setenv("DFE_TEST_TIER", "cluster")

    params = _cluster_params()

    assert params is not None
    assert params["host"] == APP_HOST


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
