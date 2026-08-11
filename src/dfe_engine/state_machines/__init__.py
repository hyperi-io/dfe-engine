#  Project:      dfe-engine
#  File:         state_machines/__init__.py
#  Purpose:      Declarative state machines for multi-step control-plane flows
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""State machines for multi-step control-plane flows.

Each module here declares the steps of a flow, the predicate that marks a
step done, and an evaluator that turns live state into a snapshot the UI can
render. The machines are pure — they take a context object, never a Request —
so they are testable without a running app.
"""
