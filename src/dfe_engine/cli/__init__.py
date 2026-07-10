#  Project:      dfe-engine
#  File:         cli/__init__.py
#  Purpose:      CLI package marker
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""dfe-engine CLI package.

The human CLI is ``dfe`` - auto-generated from the daemon's OpenAPI spec plus a
``dfe local`` break-glass group, under ``cli/auto/``. The daemon is
``dfe-engine``. This package init is intentionally empty: the old hand-written
per-resource offline commands (accounts/groups/api-keys/oidc CRUD) were removed
when the CLI consolidated to the single generated ``dfe`` - that CRUD is covered
by ``dfe auth ...`` on a running engine, and break-glass config edits go through
``dfe local`` (the same GitCrud the API uses). ``cli/ch_cloud.py`` stays - it is
mounted under ``dfe local ch-cloud``.
"""
