#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/scheduler_cases.py
#  Purpose:      Case tables for the background OIDC group sync scheduler
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the background OIDC group sync scheduler."""

from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.auth.oidc.sync import NOT_CONFIGURED_MESSAGE, SyncOutcome
from tests.unit.test_auth.factories import make_oidc_provider

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class SyncIsDueCase(TypedDict):
    id: str
    expected_due: bool
    provider: OIDCProvider


class SyncOutcomeCase(TypedDict):
    id: str
    expected_outcome: SyncOutcome
    result: dict[str, Any]


SYNC_IS_DUE_CASES: list[SyncIsDueCase] = [
    {
        "id": "disabled",
        "expected_due": False,
        "provider": make_oidc_provider(enabled=False, groups={"mode": "api"}),
    },
    {
        "id": "not_api_mode",
        "expected_due": False,
        "provider": make_oidc_provider(groups={"mode": "token_claim"}),
    },
    {
        "id": "never_synced",
        "expected_due": True,
        "provider": make_oidc_provider(groups={"mode": "api"}),
    },
    {
        "id": "synced_within_the_interval",
        "expected_due": False,
        "provider": make_oidc_provider(
            groups={"mode": "api", "sync_interval": 3600},
            last_sync_at=(NOW - timedelta(minutes=30)).isoformat(),
        ),
    },
    {
        "id": "interval_elapsed_exactly",
        "expected_due": True,
        "provider": make_oidc_provider(
            groups={"mode": "api", "sync_interval": 3600},
            last_sync_at=(NOW - timedelta(seconds=3600)).isoformat(),
        ),
    },
    {
        "id": "an_interval_under_a_minute_counts_as_a_minute",
        "expected_due": False,
        "provider": make_oidc_provider(
            groups={"mode": "api", "sync_interval": 10},
            last_sync_at=(NOW - timedelta(seconds=30)).isoformat(),
        ),
    },
    {
        "id": "a_stamp_without_a_timezone_reads_as_utc",
        "expected_due": False,
        "provider": make_oidc_provider(
            groups={"mode": "api", "sync_interval": 3600},
            last_sync_at=(NOW - timedelta(minutes=30)).replace(tzinfo=None).isoformat(),
        ),
    },
    {
        "id": "unreadable_last_sync_stamp",
        "expected_due": True,
        "provider": make_oidc_provider(groups={"mode": "api"}, last_sync_at="not-a-time"),
    },
]

SYNC_OUTCOME_CASES: list[SyncOutcomeCase] = [
    {
        "id": "every_group_synced",
        "expected_outcome": "ok",
        "result": {"created": 2, "error": None, "groups_skipped": 0, "updated": 1},
    },
    {
        "id": "some_groups_skipped",
        "expected_outcome": "partial",
        "result": {"created": 1, "error": None, "groups_skipped": 1, "updated": 0},
    },
    {
        "id": "no_directory_credential",
        "expected_outcome": "not_configured",
        "result": {
            "created": 0,
            "error": None,
            "groups_skipped": 0,
            "skipped": NOT_CONFIGURED_MESSAGE,
            "updated": 0,
        },
    },
    {
        "id": "the_run_failed",
        "expected_outcome": "error",
        "result": {
            "created": 0,
            "error": "directory unreachable",
            "groups_skipped": 0,
            "updated": 0,
        },
    },
]
