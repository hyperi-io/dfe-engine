#  Project:      dfe-engine
#  File:         tests/live/test_google_user_token.py
#  Purpose:      Google group resolution against real Cloud Identity with a real user token
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Google group resolution against real Cloud Identity, with a real user's token.

Marked ``live``, so the default run and the full tier leave it out. Select it with
``uv run pytest tests/live -m live``. It skips unless every input is in the environment.

``DFE_LIVE_GOOGLE_SA_JSON_ENV`` is the NAME of the env var that holds the service
account JSON. The service account needs domain-wide delegation for the Cloud Identity
groups read-only scope, and the Cloud Identity API enabled in its project.
``DFE_LIVE_GOOGLE_MEMBER`` is a user in every group listed in
``DFE_LIVE_GOOGLE_EXPECTED_GROUP_IDS`` (comma-separated bare group ids).
``DFE_LIVE_GOOGLE_OUTSIDER`` is a user in none of them.

The token is minted for each user by ``tests/support/google_delegation.py`` and handed to
``GoogleAdapter.resolve_user_groups``, the call a login makes. No assertion message
names a token, key, email or group id.
"""

import json
import logging
import os
from collections.abc import Iterator
from typing import Any

import pytest
from scalo.logger import logger

from dfe_engine.auth.oidc.adapters.google import GoogleAdapter
from dfe_engine.auth.oidc.models import GroupInfo, GroupResolutionConfig, OIDCProvider
from tests.support.google_delegation import mint_user_access_token

SA_JSON_ENV_VAR = "DFE_LIVE_GOOGLE_SA_JSON_ENV"
MEMBER_VAR = "DFE_LIVE_GOOGLE_MEMBER"
OUTSIDER_VAR = "DFE_LIVE_GOOGLE_OUTSIDER"
EXPECTED_GROUP_IDS_VAR = "DFE_LIVE_GOOGLE_EXPECTED_GROUP_IDS"


def _setting(name: str) -> str:
    return os.environ.get(name, "").strip()


def _missing_inputs() -> list[str]:
    """Names of the inputs not set, naming the pointer var when the JSON it points at is absent."""
    missing = [
        name for name in (MEMBER_VAR, OUTSIDER_VAR, EXPECTED_GROUP_IDS_VAR) if not _setting(name)
    ]
    if not _setting(_setting(SA_JSON_ENV_VAR)):
        missing.append(f"{SA_JSON_ENV_VAR} (naming a set env var)")
    return missing


_MISSING = _missing_inputs()

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        bool(_MISSING),
        reason=f"Needs Google Workspace sandbox: set {', '.join(_MISSING)}",
    ),
]


@pytest.fixture(autouse=True)
def _quiet_http_logs() -> Iterator[None]:
    # httpx logs each request URL at INFO, and the group lookup's URL carries the user's email.
    names = ("httpx", "httpcore")
    saved = {name: logging.getLogger(name).level for name in names}
    for name in names:
        logging.getLogger(name).setLevel(logging.WARNING)
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def _service_account() -> dict[str, Any]:
    raw = os.environ[_setting(SA_JSON_ENV_VAR)]
    try:
        info = json.loads(raw)
    except ValueError:
        pytest.fail(
            "the env var named by the service account setting is not valid JSON", pytrace=False
        )
    if not isinstance(info, dict):
        pytest.fail("the service account JSON is not an object", pytrace=False)
    return info


def _expected_group_ids() -> list[str]:
    return [
        group_id.strip()
        for group_id in _setting(EXPECTED_GROUP_IDS_VAR).split(",")
        if group_id.strip()
    ]


def _provider() -> OIDCProvider:
    """A Google provider with no service account, so only the user's own token can answer."""
    return OIDCProvider(
        type="google",
        issuer="https://accounts.google.com",
        groups=GroupResolutionConfig(enrich_on_login=True, mode="api"),
    )


async def _resolve(email: str) -> tuple[list[GroupInfo], list[dict[str, Any]]]:
    """The groups a login resolves for *email*, and the warnings the adapter logged doing it."""
    token = await mint_user_access_token(_service_account(), subject=email)
    warnings: list[dict[str, Any]] = []
    sink = logger.add(lambda message: warnings.append(message.record["extra"]), level="WARNING")
    try:
        groups = await GoogleAdapter(_provider(), access_token=token).resolve_user_groups(email)
    finally:
        logger.remove(sink)
    return groups, warnings


async def test_member_resolves_the_expected_group_ids() -> None:
    expected = _expected_group_ids()
    assert expected, f"{EXPECTED_GROUP_IDS_VAR} holds no group id"

    groups, warnings = await _resolve(_setting(MEMBER_VAR))

    resolved = {group.id for group in groups}
    absent = [position for position, group_id in enumerate(expected, 1) if group_id not in resolved]
    assert not absent, (
        f"expected group(s) at position(s) {absent} of {len(expected)} did not resolve; "
        f"{len(warnings)} warning(s) logged"
    )


async def test_resolved_ids_are_bare_group_ids() -> None:
    groups, _warnings = await _resolve(_setting(MEMBER_VAR))

    # Group files link on the Cloud Identity id, never on the ``groups/`` resource name.
    malformed = sum(1 for group in groups if not group.id or "/" in group.id)
    assert groups, "the member resolved no groups"
    assert malformed == 0, f"{malformed} of {len(groups)} resolved id(s) are not bare group ids"


async def test_outsider_resolves_none_of_the_expected_group_ids() -> None:
    expected = _expected_group_ids()

    groups, warnings = await _resolve(_setting(OUTSIDER_VAR))

    # A refused lookup also returns [], so an empty answer only counts when Cloud Identity gave one.
    refusals = [(warning.get("method"), warning.get("status")) for warning in warnings]
    assert not refusals, f"Cloud Identity gave the outsider no answer: {refusals}"
    resolved = {group.id for group in groups}
    leaked = [position for position, group_id in enumerate(expected, 1) if group_id in resolved]
    assert not leaked, f"the outsider resolved expected group(s) at position(s) {leaked}"
