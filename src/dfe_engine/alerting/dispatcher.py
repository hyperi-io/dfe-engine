#  Project:      dfe-engine
#  File:         alerting/dispatcher.py
#  Purpose:      Pluggable alert dispatcher (MVP scaffold)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Route a fired alert to its destination via a pluggable Sender.

MVP scaffold: the Sender protocol + a registry + a LogSender (no external calls).
The real senders (Slack/email/...) will be a thin wrapper over a re-use library
(Apprise is the candidate) - evaluated WHEN WE REACH this phase, not now.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from scalo.logger import logger

from .models import AlertDestination, AlertRule, fires


@runtime_checkable
class Sender(Protocol):
    """Delivers an alert payload to one destination kind."""

    kind: str

    def send(self, destination: AlertDestination, subject: str, body: str) -> bool: ...


class LogSender:
    """Default no-external-call sender (logs). Placeholder for real senders."""

    kind = "log"

    def send(self, destination: AlertDestination, subject: str, body: str) -> bool:
        logger.info("alert.dispatch", destination=destination.name, subject=subject)
        return True


class Dispatcher:
    """Evaluate a rule and, if it fires, route to its destination's sender."""

    def __init__(
        self,
        destinations: dict[str, AlertDestination],
        senders: dict[str, Sender] | None = None,
    ) -> None:
        self._destinations = destinations
        self._senders: dict[str, Sender] = senders or {"log": LogSender()}

    def dispatch(self, rule: AlertRule, count_in_window: int, subject: str, body: str) -> bool:
        """Send if the rule fires; returns whether anything was sent."""
        if not fires(rule, count_in_window):
            return False
        dest = self._destinations.get(rule.destination)
        if dest is None:
            logger.warning("alert.no_destination", rule=rule.name, destination=rule.destination)
            return False
        sender = self._senders.get(dest.kind) or self._senders.get("log")
        if sender is None:
            return False
        return sender.send(dest, subject, body)
