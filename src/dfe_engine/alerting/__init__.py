#  Project:      dfe-engine
#  File:         alerting/__init__.py
#  Purpose:      Alerting (MVP scaffold) - alert table -> external destinations
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Alerting scaffold (DEFERRED phase - see memory project_alerting).

Thin MVP only: the rule MODEL (threshold-over-window + severity->channel) and a
pluggable dispatcher. The web-research / platform survey (Apprise as the re-use
sender base; the Alertmanager routing model) happens WHEN WE REACH THIS PHASE, per
Derek - NOT now. Builds on the existing alert grouping/cooldown (dfe_audit.alert_state).
"""

from .dispatcher import Dispatcher, Sender
from .models import AlertDestination, AlertRule, fires

__all__ = [
    "AlertDestination",
    "AlertRule",
    "Dispatcher",
    "Sender",
    "fires",
]
