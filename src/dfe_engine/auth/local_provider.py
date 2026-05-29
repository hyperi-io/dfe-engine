#  Project:      dfe-engine
#  File:         auth/local_provider.py
#  Purpose:      Local authentication provider backed by AccountStore and GroupStore
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Local authentication provider backed by YAML stores.

Authenticates users against AccountStore (bcrypt passwords) and resolves
roles via GroupStore memberships.

Usage::

    from dfe_engine.auth.local_provider import LocalAuthProvider
    from dfe_engine.auth.accounts import AccountStore
    from dfe_engine.auth.groups import GroupStore

    provider = LocalAuthProvider(account_store, group_store)
    auth = provider.authenticate("admin", "changeme")
"""

from __future__ import annotations

from dfe_engine.auth.accounts import AccountStore
from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.models import AuthContext, AuthenticationError


class LocalAuthProvider:
    """Local auth backed by AccountStore + GroupStore."""

    def __init__(
        self,
        account_store: AccountStore,
        group_store: GroupStore,
    ) -> None:
        self._accounts = account_store
        self._groups = group_store

    def authenticate(
        self,
        username: str,
        password: str,
        *,
        request_id: str | None = None,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> AuthContext:
        """Verify credentials and return AuthContext with resolved roles.

        Args:
            username: Account name.
            password: Plaintext password to verify.
            request_id: Optional request correlation ID.
            client_ip: Optional client IP address.
            user_agent: Optional client user agent string.

        Returns:
            AuthContext with roles resolved from group memberships.

        Raises:
            AuthenticationError: If username unknown, account disabled,
                or password incorrect.
        """
        account = self._accounts.get(username)
        if account is None:
            # Timing-safe: verify_password does a bcrypt check even for
            # unknown users to prevent timing-based enumeration
            self._accounts.verify_password(username, password)
            raise AuthenticationError("Invalid username or password")

        if not account.enabled:
            raise AuthenticationError("Account disabled")

        if not self._accounts.verify_password(username, password):
            raise AuthenticationError("Invalid username or password")

        roles = self._groups.resolve_roles_for_member(username)

        return AuthContext(
            org_id="default",
            user_id=username,
            roles=roles,
            groups=account.groups,
            request_id=request_id,
            client_ip=client_ip,
            user_agent=user_agent,
        )
