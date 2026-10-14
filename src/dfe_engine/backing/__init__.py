#  Project:      dfe-engine
#  File:         backing/__init__.py
#  Purpose:      Who runs each backing service, and what that means for its values
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Who runs each backing service, and what the engine may change because of it.

The deployer states the provider (``deployment.backing``); the provider catalogue
says what that provider means. Together they decide whether DFE runs the service,
a provider whose API DFE knows runs it, or someone else does, and which deploy-repo
values a write cannot move.
"""
