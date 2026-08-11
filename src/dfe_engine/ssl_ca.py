#  Project:      dfe-engine
#  File:         ssl_ca.py
#  Purpose:      Ensure outbound HTTPS can verify public CAs on dev machines
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Point Python's default TLS trust store at certifi when the OS bundle is missing.

Some macOS Python installs ship without a working default CA file, which breaks
``scalo.http.AsyncHttpClient`` (and Authlib OIDC discovery) with
``CERTIFICATE_VERIFY_FAILED`` even for public IdPs like Okta. Setting
``SSL_CERT_FILE`` early fixes every consumer that uses ``ssl.create_default_context()``.
"""

from __future__ import annotations

import os


def ensure_platform_ssl_ca_bundle() -> None:
    """Set ``SSL_CERT_FILE`` from certifi when no CA bundle env is configured."""
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE"):
        return
    try:
        import certifi
    except ImportError:
        return
    os.environ["SSL_CERT_FILE"] = certifi.where()
