#  Project:      dfe-engine
#  File:         cli/auto/__init__.py
#  Purpose:      Auto-generated `dfe` HTTP CLI - package marker
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The ``dfe`` CLI: a command tree auto-generated from the engine OpenAPI spec.

Every API operation (unless it opts out via ``x-cli.enabled == false``) becomes a
CLI command that speaks HTTP to a running ``dfe-engine`` daemon. Adding an endpoint
yields a CLI command with zero CLI code. This is the generated HTTP-client half of
the hybrid; the break-glass half (``dfe local``, for when the daemon is down) is
mounted alongside it in ``local.py``.
"""
