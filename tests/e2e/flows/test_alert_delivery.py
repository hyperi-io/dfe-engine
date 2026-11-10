#  Project:      dfe-engine
#  File:         tests/e2e/flows/test_alert_delivery.py
#  Purpose:      A hunt that matches ingested data delivers its alert to the destination
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A record in, a hunt fire on it, and the alert out at its destination.

The destination is the deployment's own receiver: Apprise's ``json://`` POSTs the
alert there, the receiver lands it in the catch-all like any unmatched record, and
the case reads it back out of ClickHouse by the hunt's name. That needs no sink the
suite has to stand up, and it proves the whole chain a deployment runs: the engine
writes the destination to the secrets backend (or its mirror Secret), the runner
reads it from there, fires on the rule, and Apprise reaches the URL from the
runner's own network.

The runner reads hunts and rules off its git-sync of the deploy repo, so a new hunt
is minutes away. The case therefore keeps posting marked records until an alert
lands, rather than posting once and racing the sync: a record posted after the
runner picked the hunt up lands in a window it reads.

``DFE_E2E_ALERT_SINK_URL`` is the receiver URL as the runner reaches it, when that
differs from ``DFE_E2E_RECEIVER_URL`` (an in-cluster Service name, say). Unset, the
runner is handed the receiver URL the suite posts to.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote, urlsplit

import pytest

from tests.e2e.conftest import PAYLOAD_TEXT, E2EConfig, count_rows, poll_until, post_events, require
from tests.e2e.engine_api import EngineAPI
from tests.e2e.flows import shapes

# The runner's git-sync period, its reload tick and Argo's poll each stand between a
# hunt write and its first fire, so the first alert is minutes away.
DELIVERY_DEADLINE = 1200.0
# How often the case posts a fresh marked record while it waits for the first alert.
PROBE_INTERVAL = 30.0
# A record's trip to the catch-all, once routing is settled, which it is for the catch-all.
LANDING_DEADLINE = 240.0

pytestmark = [pytest.mark.live, pytest.mark.timeout(DELIVERY_DEADLINE + LANDING_DEADLINE + 300.0)]


def _token() -> str:
    return uuid.uuid4().hex[:12]


def apprise_url(receiver_url: str, *, token: str | None, verify: bool) -> str:
    """The Apprise ``json://`` URL that POSTs an alert to *receiver_url*.

    Args:
        receiver_url: The receiver's ingest URL as the runner reaches it.
        token: The receiver's bearer token, sent as an Authorization header when set.
        verify: Whether the runner checks the receiver's TLS certificate.

    Returns:
        ``json://`` for http and ``jsons://`` for https, with the same host, port
        and path, and Apprise's header and verify options as query parameters.
    """
    parts = urlsplit(receiver_url)
    scheme = "jsons" if parts.scheme == "https" else "json"
    options = [] if verify else ["verify=no"]
    if token:
        options.append(f"+Authorization={quote(f'Bearer {token}', safe='')}")
    query = f"?{'&'.join(options)}" if options else ""
    return f"{scheme}://{parts.netloc}{parts.path or '/'}{query}"


def _tenant_org(engine: EngineAPI, ch_client, table: str, marker: str) -> str:
    """The registered org whose tenant ids hold the ``_org_id`` the receiver stamped.

    Read off a landed record rather than assumed: whether the suite's posts land in
    the default org depends on whether the deployment authenticates them.
    """
    rows = ch_client.query(
        f"SELECT _org_id FROM {table} WHERE {PAYLOAD_TEXT} LIKE %(m)s LIMIT 1",
        parameters={"m": f"%{marker}%"},
    ).result_rows
    assert rows, f"the probe record is not in {table}"
    tenant = str(rows[0][0])
    orgs = engine.json("GET", "/orgs?page_size=1000")["items"]
    owners = [str(org["name"]) for org in orgs if tenant in org["org_ids"]]
    assert owners, f"no registered org holds the tenant id {tenant!r} the receiver stamped"
    return owners[0]


@pytest.fixture
def hunt_parts(engine: EngineAPI) -> Iterator[dict[str, str]]:
    """The rule, hunt and destination this case creates, removed however it ended."""
    token = _token()
    parts = {
        "rule": f"alert_flow_rule_{token}",
        "hunt": f"alert_flow_hunt_{token}",
        "destination": f"alert-flow-{token}",
    }
    yield parts
    # The hunt delete takes the destination it owns with it.
    for path in (
        f"/hunts/{parts['hunt']}",
        f"/alerts/destinations/{parts['destination']}",
        f"/rules/{parts['rule']}",
    ):
        response = engine.call("DELETE", path)
        assert response.status_code in (200, 204, 404), (
            f"could not remove {path}: {response.status_code} {response.text}"
        )


def _skip_on_review(response: Any, what: str) -> None:
    """Skip, declared, where the deployment routes the write to a review branch.

    The runner git-syncs the branch the engine commits to, so a write held for review
    never reaches it and there is no fire to wait for.
    """
    if response.headers.get("X-DFE-Review-Required") == "true":
        pytest.skip(
            f"{shapes.EXPECTED_SKIP} this deployment routes the {what} write to a review "
            "branch, so the hunt runner never sees it until a person merges it"
        )


class TestAlertDelivery:
    """One hunt, one destination, one alert at the receiver."""

    def test_a_hunt_match_reaches_its_alert_destination(
        self,
        engine: EngineAPI,
        e2e: E2EConfig,
        ch_client,
        catchall: tuple[str, str],
        hunt_parts: dict[str, str],
    ) -> None:
        require(e2e, "receiver_url", "ch_host")
        status = engine.json("GET", "/hunts/status")
        assert status["runners"] > 0, (
            f"the deployment reports no live hunt runner, so no hunt can fire: {status}"
        )
        table = f"{e2e.ch_db}.{catchall[1]}"
        marker = f"alertflow{_token()}"

        def post_probe() -> None:
            post_events(e2e, [{"message": f"alert flow {marker}", "host": {"name": "alert-e2e"}}])

        post_probe()
        poll_until(
            lambda: count_rows(ch_client, table, marker=marker),
            timeout=LANDING_DEADLINE,
            desc=f"the probe record in {table}",
        )
        customer = _tenant_org(engine, ch_client, table, marker)

        rule = engine.call(
            "POST",
            "/rules",
            {
                "name": hunt_parts["rule"],
                "severity": "low",
                "user_sql": (f"SELECT * FROM {table} WHERE {PAYLOAD_TEXT} LIKE '%{marker}%'"),
                "estimate_cost": False,
            },
        )
        assert rule.status_code == 201, f"the rule was refused: {rule.status_code} {rule.text}"
        _skip_on_review(rule, "rule")
        hunt = engine.call(
            "POST",
            "/hunts",
            {
                "name": hunt_parts["hunt"],
                "cron": "* * * * *",
                "customers": [customer],
                "rules": [hunt_parts["rule"]],
            },
        )
        assert hunt.status_code == 201, f"the hunt was refused: {hunt.status_code} {hunt.text}"
        _skip_on_review(hunt, "hunt")

        sink = os.getenv("DFE_E2E_ALERT_SINK_URL") or e2e.receiver_url
        assert sink is not None
        destination = engine.call(
            "POST",
            "/alerts/destinations",
            {
                "name": hunt_parts["destination"],
                "url": apprise_url(sink, token=e2e.receiver_token, verify=e2e.verify),
                "description": "alert delivery e2e flow",
                "hunt_name": hunt_parts["hunt"],
            },
        )
        assert destination.status_code == 201, (
            f"the destination was refused: {destination.status_code} {destination.text}"
        )
        _skip_on_review(destination, "hunt link")

        def delivered() -> int:
            # A fresh record each pass, so one posted after the runner loaded the hunt exists.
            post_probe()
            return count_rows(ch_client, table, contains=hunt_parts["hunt"])

        alerts = poll_until(
            delivered,
            timeout=DELIVERY_DEADLINE,
            interval=PROBE_INTERVAL,
            desc=(
                f"an alert naming {hunt_parts['hunt']} at {sink}, posted by the hunt runner "
                f"and landed in {table}"
            ),
        )
        assert alerts > 0
        title = count_rows(ch_client, table, contains=f"DFE Hunt Alert: {hunt_parts['hunt']}")
        assert title > 0, (
            "a record naming the hunt landed, but none carries the alert's title, so it "
            "is not the runner's notification"
        )
