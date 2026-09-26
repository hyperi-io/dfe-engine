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

from typing import Any

from fastapi.testclient import TestClient

from dfe_engine.auth.bootstrap import admin_account_name


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

    def __enter__(self) -> TestClient:
        client = super().__enter__()
        admin_on_its_own_password(self.app)
        return client
