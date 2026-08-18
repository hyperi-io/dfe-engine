#  Project:      dfe-engine
#  File:         auth/issuer/factory.py
#  Purpose:      Build the configured issuer backend (the impl-choice seam)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Build the configured issuer backend.

The one place that picks a concrete issuer implementation from config. Today
that is always dex; a new issuer is a new branch here and nothing else changes,
because the rest of the engine holds only an :class:`IssuerBackend`.
"""

from __future__ import annotations

from scalo.logger import logger

from dfe_engine.auth.issuer.backend import IssuerBackend


def build_issuer_backend(settings) -> IssuerBackend | None:
    """Return the configured issuer backend, or None when disabled/unconfigured.

    Disabled (the default) returns None so the management plane is simply off.
    Enabled-but-unconfigured logs a warning and returns None rather than raising,
    so a misconfiguration cannot break engine startup.
    """
    issuer = settings.issuer
    if not issuer.enabled:
        return None
    if not (issuer.endpoint and issuer.ca_cert and issuer.client_cert and issuer.client_key):
        logger.warning(
            "issuer management enabled but endpoint/mTLS material incomplete; disabling",
            endpoint=issuer.endpoint or "",
        )
        return None

    from dfe_engine.auth.issuer.dex.client import DexIssuerBackend

    logger.info("issuer management plane enabled", endpoint=issuer.endpoint)
    return DexIssuerBackend(
        endpoint=issuer.endpoint,
        ca_cert=issuer.ca_cert,
        client_cert=issuer.client_cert,
        client_key=issuer.client_key,
        server_name=issuer.server_name,
    )
