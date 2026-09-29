#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_login_backoff.py
#  Purpose:      POST /auth/login backs off failed sign-ins before it checks a password
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The sign-in throttle through the real login route.

A waiting attempt is refused with 429 before the password is checked, so the right
password waits too, and a flood of waiting attempts costs no bcrypt.
"""

import secrets

from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from scalo.metrics import create_metrics

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
from dfe_engine.api.metrics import SIGN_IN_THROTTLED

_LOGIN = "/api/v1/auth/login"


def _attempt(client, username: str, password: str):
    return client.post(_LOGIN, json={"username": username, "password": password})


def _account(app) -> str:
    password = secrets.token_urlsafe(16)
    app.state.account_store.create("carol", password)
    return password


def test_five_failures_make_the_next_attempt_wait(client, app, audit_events):
    password = _account(app)
    failures = [_attempt(client, "carol", "wrong-password").status_code for _ in range(5)]

    waiting = _attempt(client, "carol", password)

    assert failures == [401] * 5
    assert waiting.status_code == 429, waiting.text
    assert waiting.json()["code"] == "too_many_attempts"
    assert int(waiting.headers["Retry-After"]) >= 1
    assert any(
        e["event"] == "auth.login.denied" and e.get("reason") == "throttled" for e in audit_events
    )


def test_a_waiting_attempt_checks_no_password(client, app, monkeypatch):
    _account(app)
    for _ in range(5):
        _attempt(client, "carol", "wrong-password")
    checked: list[str] = []
    real = app.state.account_store.verify_password

    def counting(username, password):
        checked.append(username)
        return real(username, password)

    monkeypatch.setattr(app.state.account_store, "verify_password", counting)

    for _ in range(20):
        assert _attempt(client, "carol", "wrong-password").status_code == 429

    assert checked == []


def test_an_unknown_username_backs_off_like_a_real_one(client):
    statuses = [_attempt(client, "nobody-here", "wrong-password").status_code for _ in range(6)]

    assert statuses == [401] * 5 + [429]


def test_a_success_before_the_threshold_clears_the_count(client, app):
    password = _account(app)
    for _ in range(4):
        _attempt(client, "carol", "wrong-password")

    assert _attempt(client, "carol", password).status_code == 200
    statuses = [_attempt(client, "carol", "wrong-password").status_code for _ in range(4)]

    assert statuses == [401] * 4
    assert _attempt(client, "carol", password).status_code == 200


def test_another_username_is_not_held_back(client, app):
    _account(app)
    for _ in range(5):
        _attempt(client, "carol", "wrong-password")

    assert _attempt(client, "operator", "test-operator-pw").status_code == 200


def test_each_waiting_attempt_is_counted_by_route(api_settings):
    manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
    app = create_app(settings=api_settings, metrics_manager=manager)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            for _ in range(8):
                _attempt(client, "nobody-here", "wrong-password")
    finally:
        _registries.clear()

    samples = [
        sample
        for family in text_string_to_metric_families(manager.metrics_text)
        for sample in family.samples
        if sample.name == SIGN_IN_THROTTLED
    ]
    assert [(s.labels, s.value) for s in samples] == [({"route": "/auth/login"}, 3.0)]
