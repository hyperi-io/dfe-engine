#  Project:      dfe-engine
#  File:         auth/issuer/dex/__init__.py
#  Purpose:      Package marker for the dex implementation of the issuer backend
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Dex implementation of the generic issuer backend.

Dex-specific code lives here (the vendored proto + generated stubs + the gRPC
client). Everything OUTSIDE this package refers to the issuer through the
generic interface in ``dfe_engine.auth.issuer.backend`` -- so swapping dex for
another issuer never reaches the engine API or CLI.
"""
