#  Project:      dfe-engine
#  File:         alerting/models.py
#  Purpose:      Alert rule + destination models (MVP scaffold)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Alert rule + destination models (Governed Ops gitops resources).

Start small with the 90% cases: a threshold over a window, and severity->channel
routing. These are gitops CRUD resources like everything else.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AlertDestination(BaseModel):
    """An external sink, e.g. a Slack channel. Secrets via secret_ref, not inline."""

    name: str
    kind: str = "slack"  # slack | email | webhook | ... (sender chosen at dispatch)
    target: str = ""  # channel / url / address
    secret_ref: str = ""  # env/secret name holding the token


class AlertRule(BaseModel):
    """Fire to a destination when matched rows in a window cross a threshold.

    e.g. "only alert when >1000 of these in the last hour" -> threshold=1000,
    window_seconds=3600; severity routes to a channel.
    """

    name: str
    description: str = ""
    severity: str = "info"  # info | warning | critical
    threshold: int = 1  # min matched rows in the window to fire
    window_seconds: int = 3600
    destination: str = ""  # AlertDestination name
    labels: dict[str, str] = Field(default_factory=dict)


def fires(rule: AlertRule, count_in_window: int) -> bool:
    """Does this rule fire given the matched-row count over its window?"""
    return count_in_window >= rule.threshold
