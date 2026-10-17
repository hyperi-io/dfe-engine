#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_permission_covers.py
#  Purpose:      permission_covers holds exactly when every matched action is matched by the holder
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""``permission_covers(held, requested)`` against the matcher it must agree with."""

import itertools

import pytest

from dfe_engine.auth.roles import permission_covers, permission_matches

COVERS = [
    ("*", "*"),
    ("*", "config:read"),
    ("config:*", "config:*"),
    ("config:*", "config:read"),
    ("config:*", "config:read:sub"),
    ("config:*", "config:read:*"),
    ("service:*:config:*", "service:loader:config:read"),
    ("service:*:config:*", "service:*:config:read"),
    ("service:*:config:read", "service:loader:config:read"),
    ("group:read", "group:read"),
]

DOES_NOT_COVER = [
    ("config:*", "*"),
    ("config:*", "config"),
    ("config:*", "hunt:read"),
    ("config:read", "config:*"),
    ("config:read", "config:write"),
    ("config:read", "*"),
    ("service:loader:config:read", "service:*:config:read"),
    ("service:*:config:read", "service:*:config:*"),
    ("service:*:config:read", "service:loader:config:read:extra"),
    ("group:read", "group:read:sub"),
]


@pytest.mark.parametrize(("held", "requested"), COVERS)
def test_covers(held: str, requested: str):
    assert permission_covers(held, requested) is True


@pytest.mark.parametrize(("held", "requested"), DOES_NOT_COVER)
def test_does_not_cover(held: str, requested: str):
    assert permission_covers(held, requested) is False


ACTIONS = [
    "config",
    "config:read",
    "config:write",
    "config:read:sub",
    "hunt:read",
    "service:loader:config:read",
    "service:loader:config:write",
    "service:loader:metrics:read",
    "service:loader:config:read:extra",
    "*",
]


@pytest.mark.parametrize(("held", "requested"), COVERS)
def test_a_covered_pattern_matches_nothing_the_holder_does_not(held: str, requested: str):
    """Soundness against the matcher: no sample action escapes a claimed cover."""
    for action in ACTIONS:
        if permission_matches(requested, action):
            assert permission_matches(held, action), action


def test_no_pattern_in_the_cases_covers_one_that_matches_more():
    patterns = {pattern for pair in COVERS + DOES_NOT_COVER for pattern in pair}
    for held, requested in itertools.product(patterns, repeat=2):
        if not permission_covers(held, requested):
            continue
        escaped = [
            action
            for action in ACTIONS
            if permission_matches(requested, action) and not permission_matches(held, action)
        ]
        assert escaped == [], (held, requested)
