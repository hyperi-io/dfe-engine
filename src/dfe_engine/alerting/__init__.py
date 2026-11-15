#  Project:      dfe-engine
#  File:         alerting/__init__.py
#  Purpose:      Alert rule model and dispatcher to external destinations
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Alert rule and destination models, and a dispatcher that routes a fired rule.

A rule fires when its matched-row count over a window reaches its threshold. The
dispatcher hands the alert to the ``Sender`` registered for the destination's kind
and falls back to ``LogSender`` when there is none. ``LogSender`` is the only
sender shipped: it logs the alert and reports it sent, without delivering it.
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
