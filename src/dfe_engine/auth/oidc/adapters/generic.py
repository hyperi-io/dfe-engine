#  Project:      dfe-engine
#  File:         auth/oidc/adapters/generic.py
#  Purpose:      Generic OIDC adapter for providers without an admin API
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Generic OIDC group adapter.

Used when the provider is ``type: generic`` or as a safe fallback for
provider types whose dedicated adapter has not yet been implemented.

Group resolution uses the OIDC token claim (resolved upstream by Envoy
Gateway or the token validator).  No admin API calls are made.
"""

from __future__ import annotations

from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
from dfe_engine.auth.oidc.models import GroupInfo


class GenericAdapter(OIDCGroupAdapter):
    """Adapter for generic OIDC providers with no admin API.

    ``list_all_groups`` always returns an empty list -- there is no API to
    enumerate groups from a generic provider.

    ``test_connection`` always succeeds -- there is no connection to test.
    """

    async def list_all_groups(self) -> list[GroupInfo]:
        """Return an empty list -- generic providers have no enumeration API.

        Returns:
            An empty list.
        """
        return []

    async def test_connection(self) -> tuple[bool, str]:
        """Report success with an informational message.

        Returns:
            (True, "Generic provider -- no API to test")
        """
        return (True, "Generic provider -- no API to test")
