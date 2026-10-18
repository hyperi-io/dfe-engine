#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_platform_probe.py
#  Purpose:      clickhouse.platform is the probe's override layer, and a pinned caller beats it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every DDL path asks ``platform_probe``, so the operator's setting has to reach it.

No server is reached: an override and a hostname resolve before the probe would ask
one, so a client that answers nothing proves the cascade stopped where it should.
"""

import pytest
from dfe_schemas.clickhouse import Platform

from dfe_engine.schema.plan import platform_probe
from dfe_engine.settings import load_settings


class _Unreachable:
    """A client the probe must not need: every query fails."""

    def query(self, sql: str, parameters=None):
        raise ConnectionError("the server was asked")


def _settings(*, platform: str, host: str):
    settings = load_settings().model_copy(deep=True)
    settings.clickhouse.platform = platform
    settings.clickhouse.host = host
    return settings


@pytest.mark.parametrize(
    ("configured", "expected"), [("cloud", Platform.CLOUD), ("self_hosted", Platform.SELF_HOSTED)]
)
def test_the_configured_platform_pins_the_probe(configured, expected):
    probe = platform_probe(_settings(platform=configured, host="ch.internal"), _Unreachable())

    assert probe.platform is expected
    assert probe.origin == "override"


def test_auto_leaves_the_cascade_to_decide():
    probe = platform_probe(_settings(platform="auto", host="abc.clickhouse.cloud"), None)

    assert probe.platform is Platform.CLOUD
    assert probe.origin == "hostname"


def test_a_caller_that_pins_the_platform_beats_the_setting():
    settings = _settings(platform="cloud", host="ch.internal")

    probe = platform_probe(settings, _Unreachable(), platform=Platform.SELF_HOSTED)

    assert probe.platform is Platform.SELF_HOSTED
