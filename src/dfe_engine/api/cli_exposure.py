#  Project:      dfe-engine
#  File:         api/cli_exposure.py
#  Purpose:      OpenAPI x-cli extension - marks endpoints the auto-CLI skips
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""``x-cli`` OpenAPI extension - the SSoT for which endpoints the auto-CLI exposes.

The ``dfe`` CLI is generated from the engine's OpenAPI spec: every operation
becomes a command UNLESS it carries ``x-cli.enabled == false``. Default is
enabled, so an endpoint has to opt OUT - a new API is a new CLI command with no
CLI code (the botocore model -> command pattern, applied to OpenAPI).

Attach ``CLI_HIDDEN`` to a route's ``openapi_extra`` for endpoints that make no
sense as a human CLI command:
- machine/scaler endpoints (KEDA ``hunts-due``),
- UI-only surfaces (client-config bootstrap, the UI-prefs repository store),
- flows the CLI handles specially as a built-in (``auth login``/``refresh`` ->
  ``dfe auth login``, which stores the returned token in the credential file).

    @router.get("/client", response_model=ClientConfig, openapi_extra=CLI_HIDDEN)
"""

from __future__ import annotations

from typing import Any

# openapi_extra fragment: opt an operation OUT of the generated CLI.
CLI_HIDDEN: dict[str, Any] = {"x-cli": {"enabled": False}}


def cli_enabled(operation: dict[str, Any]) -> bool:
    """True unless the OpenAPI operation opted out via ``x-cli.enabled == false``.

    Default-on: an operation with no ``x-cli`` block is exposed. Only an explicit
    ``{"x-cli": {"enabled": False}}`` hides it.
    """
    xcli = operation.get("x-cli")
    if isinstance(xcli, dict):
        return xcli.get("enabled", True) is not False
    return True
