#  Project:      dfe-engine
#  File:         tests/unit/test_e2e_harness/test_alert_sink_url.py
#  Purpose:      The Apprise URL the alert delivery flow hands the hunt runner
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The alert flow's destination URL, parsed by the Apprise the runner runs.

A URL Apprise reads differently from what the flow meant sends the alert somewhere
else, and the live case can only report that as a timeout twenty minutes later.
"""

import apprise
import pytest
from apprise.plugins.custom_json import NotifyJSON

from tests.e2e.flows.test_alert_delivery import apprise_url


def _parsed(url: str) -> NotifyJSON:
    """The plugin Apprise builds for *url*, which must be its JSON POST plugin."""
    plugin = apprise.Apprise.instantiate(url)
    assert isinstance(plugin, NotifyJSON), f"Apprise read {url!r} as {plugin!r}"
    return plugin


@pytest.mark.parametrize(
    ("receiver", "secure", "host", "port", "path"),
    [
        pytest.param(
            "https://ingest.example.test/ingest",
            True,
            "ingest.example.test",
            None,
            "/ingest",
            id="https",
        ),
        pytest.param(
            "http://dfe-receiver.dfe.svc:8080/v1/ingest",
            False,
            "dfe-receiver.dfe.svc",
            8080,
            "/v1/ingest",
            id="http-port",
        ),
        pytest.param("http://receiver", False, "receiver", None, "/", id="no-path"),
    ],
)
def test_apprise_posts_to_the_receiver_the_flow_names(receiver, secure, host, port, path):
    plugin = _parsed(apprise_url(receiver, token=None, verify=True))

    assert plugin.secure is secure
    assert plugin.host == host
    assert plugin.port == port
    assert plugin.fullpath == path
    assert plugin.verify_certificate is True
    assert plugin.headers == {}


def test_the_bearer_token_rides_as_an_authorization_header():
    plugin = _parsed(
        apprise_url("https://ingest.example.test/ingest", token="t0k/en+x", verify=True)
    )

    assert plugin.headers == {"Authorization": "Bearer t0k/en+x"}


def test_an_unverified_run_tells_apprise_not_to_check_the_certificate():
    plugin = _parsed(apprise_url("https://ingest.example.test/ingest", token=None, verify=False))

    assert plugin.verify_certificate is False
