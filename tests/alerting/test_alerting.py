#  Project:      dfe-engine
#  File:         tests/alerting/test_alerting.py
#  Purpose:      Tests for the alerting scaffold (rule eval + dispatcher routing)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Alerting MVP: threshold firing + pluggable dispatch."""

from __future__ import annotations

from dfe_engine.alerting import AlertDestination, AlertRule, Dispatcher, fires
from dfe_engine.alerting.dispatcher import Sender


def test_fires_at_threshold():
    rule = AlertRule(name="r", threshold=1000, window_seconds=3600)
    assert fires(rule, 1000) is True
    assert fires(rule, 999) is False


class _RecordingSender:
    kind = "slack"

    def __init__(self):
        self.sent = []

    def send(self, destination, subject, body):
        self.sent.append((destination.name, subject))
        return True


def test_dispatch_sends_when_rule_fires():
    dest = AlertDestination(name="soc", kind="slack", target="#soc")
    sender = _RecordingSender()
    d = Dispatcher({"soc": dest}, senders={"slack": sender})
    rule = AlertRule(name="r", threshold=10, destination="soc")
    assert d.dispatch(rule, count_in_window=50, subject="s", body="b") is True
    assert sender.sent == [("soc", "s")]


def test_dispatch_noop_below_threshold():
    dest = AlertDestination(name="soc", kind="slack")
    sender = _RecordingSender()
    d = Dispatcher({"soc": dest}, senders={"slack": sender})
    rule = AlertRule(name="r", threshold=100, destination="soc")
    assert d.dispatch(rule, count_in_window=5, subject="s", body="b") is False
    assert sender.sent == []


def test_dispatch_missing_destination_is_safe():
    d = Dispatcher({}, senders={"slack": _RecordingSender()})
    rule = AlertRule(name="r", threshold=1, destination="absent")
    assert d.dispatch(rule, count_in_window=10, subject="s", body="b") is False


def test_sender_protocol_is_structural():
    # _RecordingSender satisfies the Sender protocol without inheritance
    assert isinstance(_RecordingSender(), Sender)
