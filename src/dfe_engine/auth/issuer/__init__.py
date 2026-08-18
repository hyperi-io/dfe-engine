#  Project:      dfe-engine
#  File:         auth/issuer/__init__.py
#  Purpose:      Generic identity-issuer management plane (issuer-agnostic)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Generic identity-issuer management plane.

The engine manages the bundled OIDC issuer through the issuer-agnostic
interface defined in ``backend`` -- users, connectors and sessions in engine
terms, never issuer-specific vocabulary. ``dex`` is one implementation; the API
and CLI depend only on the interface, so changing issuers is not an API change.
"""
