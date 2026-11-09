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

from dfe_engine.auth.accounts import Account, AccountStore
from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.models import AuthContext, AuthenticationError


def _same_standing(checked: Account, current: Account | None) -> bool:
    """Whether *current* still grants what *checked* was judged on.

    The session marker moves with the password and every session end. The rest is
    compared because a record written verbatim can change it without moving the marker.
    """
    return current is not None and (
        current.session_marker(),
        current.session_denied(),
        current.external,
        current.org,
        current.groups,
    ) == (
        checked.session_marker(),
        checked.session_denied(),
        checked.external,
        checked.org,
        checked.groups,
    )


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
            AuthContext with roles resolved from group memberships, and the org the
            account is bound to as its ``org_id`` (empty for none).

        Raises:
            AuthenticationError: "Invalid username or password" for an unknown,
                external or soft-deleted account or a wrong password. The blocked
                or disabled reason comes only once the password is right.
        """
        # The password and the state are both judged on this one record.
        account = self._accounts.get(username)
        # One bcrypt check on every path, and no account state reported before it
        # passes, so neither the answer nor its timing tells a guesser the account
        # exists or what state it is in.
        verified = self._accounts.verify_account_password(account, password)

        # External (IdP-owned / JIT-provisioned) identities log in only through the
        # trusted OIDC path, so a password they hold locally is never accepted here.
        if account is None or account.external or account.is_soft_deleted or not verified:
            raise AuthenticationError("Invalid username or password")

        denied = account.session_denied()
        if denied is not None:
            raise AuthenticationError(denied[1])

        # The store has no lock, so a change made while bcrypt ran is caught here, and
        # one made after is refused on the token's first use by get_current_user.
        if not _same_standing(account, self._accounts.get(username)):
            raise AuthenticationError("Invalid username or password")

        roles = self._groups.resolve_roles_for_member(username)

        return AuthContext(
            org_id=account.org,
            user_id=username,
            roles=roles,
            groups=account.groups,
            request_id=request_id,
            client_ip=client_ip,
            user_agent=user_agent,
            session_marker=account.session_marker(),
        )
