#  Project:      dfe-engine
#  File:         tests/support/orgs.py
#  Purpose:      Name an org as the deployment's own in API tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Name an org as the deployment's own, which completes the setup wizard's organisations step."""

from fastapi import FastAPI

from tests.support.present import present


def name_your_org(*, app: FastAPI, display_name: str = "Acme", name: str = "acme") -> None:
    """Register *name* and name it as *app*'s own org."""
    app.state.org_registry.create(name, display_name=display_name)
    present(app.state.org_registry.home).set(actor="test", name=name)
