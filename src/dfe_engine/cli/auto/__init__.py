#  Project:      dfe-engine
#  File:         cli/auto/__init__.py
#  Purpose:      Auto-generated `dfe` HTTP CLI - package marker
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The ``dfe`` CLI: scalo's ``SpecCli`` over the engine OpenAPI spec.

Every API operation (unless ``x-scalo.cli.hidden`` is set) becomes a CLI command
that speaks HTTP to a running ``dfe-engine`` daemon. Adding an endpoint yields a CLI
command with zero CLI code. The commands with no operation behind them (``login``,
``config``, and the daemon-free ``dfe local``, ``dfe schema`` and
``dfe kafka client-config``) are mounted alongside in ``app.py``.
"""
