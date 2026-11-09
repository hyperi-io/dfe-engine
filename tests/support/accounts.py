#  Project:      dfe-engine
#  File:         tests/support/accounts.py
#  Purpose:      The bootstrap admin as a test of some other router needs it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The bootstrap admin after its owner has replaced the issued password.

A booted app issues the admin its password with a forced change, and every route
but the change refuses it until then. A test of some other router mints an admin
token and wants that router's answer, so it starts from the admin a completed
first login leaves. The forced change itself is tested in
``tests/unit/test_api/test_password_change_gate.py``.
"""

from typing import Any, Self, override

from fastapi.testclient import TestClient

from dfe_engine.api.deps import account_for_session_subject, create_access_token
from dfe_engine.auth.bootstrap import admin_account_name
from dfe_engine.auth.sessions import SESSION_CLAIM


def session_token(app: Any, settings: Any, data: dict[str, Any]) -> str:
    """A token for ``data["sub"]`` carrying the session marker its account holds now.

    A token for a subject that binds no account carries the empty marker, which every
    route refuses, as it refuses a real session for no account.

    Args:
        app: A FastAPI app whose lifespan has run, so its account store exists.
        settings: The settings the app signs its tokens with.
        data: The claims, ``sub`` included.

    Returns:
        The signed token.
    """
    account = account_for_session_subject(app.state.account_store, data["sub"])
    marker = account.session_marker() if account is not None else ""
    return create_access_token(data={**data, SESSION_CLAIM: marker}, settings=settings)


def admin_on_its_own_password(app: Any) -> None:
    """Clear the bootstrap admin's forced change, leaving its password as it is.

    Args:
        app: A FastAPI app whose lifespan has run, so its account store exists.
    """
    store = app.state.account_store
    name = admin_account_name(app.state.settings.auth.local.admin_name)
    account = store.get(name)
    if account is None or not account.password_change_required:
        return
    store.put(account.model_copy(update={"password_change_required": False}), allow_protected=True)


class OnboardedClient(TestClient):
    """A client whose app starts with the admin past its first login."""

    @override
    def __enter__(self) -> Self:
        client = super().__enter__()
        admin_on_its_own_password(self.app)
        return client
