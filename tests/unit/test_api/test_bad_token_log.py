#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_bad_token_log.py
#  Purpose:      A burst of refused bearer tokens logs a bounded number of reasons
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A refused bearer token is logged twice: its audit event, and the library's reason.

A client retrying an expired token, or anyone spraying forged ones, would write the
reason once per request. The reason line goes through scalo's rate limit, so a burst
writes one, while the audit event stays one per request and carries the reference
the answer names. The counter is exact either way.
"""

from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from scalo.metrics import create_metrics

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
from dfe_engine.api.metrics import BEARER_TOKENS_REFUSED

BURST = 25


def _refuse(client: TestClient, count: int) -> list[str]:
    """Send ``count`` bad tokens and return each answer's message."""
    messages = []
    for i in range(count):
        resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer forged-{i}"})
        assert resp.status_code == 401, resp.text
        messages.append(resp.json()["message"])
    return messages


def test_a_burst_of_bad_tokens_logs_the_reason_a_bounded_number_of_times(client, audit_events):
    _refuse(client, BURST)

    reasons = [e for e in audit_events if e["event"].startswith("bearer token refused")]
    assert len(reasons) == 1
    assert reasons[0]["error"]


def test_every_refusal_keeps_its_audit_event_and_its_reference(client, audit_events):
    messages = _refuse(client, BURST)

    denied = [
        e
        for e in audit_events
        if e["event"] == "auth.login.denied" and e.get("login_method") == "jwt"
    ]
    assert len(denied) == BURST
    references = {e["reference"] for e in denied}
    assert all(any(ref in message for ref in references) for message in messages)
    assert len(references) == BURST


def test_every_refused_token_is_counted(api_settings):
    manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
    app = create_app(settings=api_settings, metrics_manager=manager)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            _refuse(client, BURST)
    finally:
        _registries.clear()

    samples = [
        sample
        for family in text_string_to_metric_families(manager.metrics_text)
        for sample in family.samples
        if sample.name == BEARER_TOKENS_REFUSED
    ]
    assert [(s.labels, s.value) for s in samples] == [({"reason": "DecodeError"}, float(BURST))]
