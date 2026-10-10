#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_builtin_role_grants.py
#  Purpose:      Every built-in role grant names an action the scope catalogue defines
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Built-in role grants checked against the scope catalogue the routes enforce.

A grant naming a domain the catalogue does not define matches no action a route
checks, so the role silently lacks what it was written to hold.
"""

import pytest

from dfe_engine.auth.engine import (
    API_ENFORCED_ACTIONS,
    ARGO_ACTION_PREFIX,
    ENGINE_ACTIONS,
    authorize,
)
from dfe_engine.auth.models import AuthContext
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.auth.roles import RoleConfig
from dfe_engine.governance.models import ActionDef

# A governed action derives the action it requires from its name; any name stands for all.
_GOVERNED_ACTION = ActionDef(name="probe").required_action


def _catalogue_actions() -> set[str]:
    """Every action the catalogue names, never counting the built-in roles themselves."""
    return {
        *scopes_dict.values(),
        *ENGINE_ACTIONS,
        *API_ENFORCED_ACTIONS,
        _GOVERNED_ACTION,
    }


def _catalogue_domains() -> set[str]:
    domains = {action.split(":")[0] for action in _catalogue_actions()}
    domains.add(ARGO_ACTION_PREFIX.rstrip(":"))
    return domains


def _builtin_grants() -> list[tuple[str, str]]:
    roles = RoleConfig.load_builtin().roles
    return [
        (name, grant) for name, role in roles.items() for grant in role.permissions if grant != "*"
    ]


def test_every_builtin_grant_names_a_catalogue_domain():
    domains = _catalogue_domains()
    unknown = [
        (role, grant) for role, grant in _builtin_grants() if grant.split(":")[0] not in domains
    ]
    assert unknown == []


def test_every_exact_builtin_grant_is_a_catalogue_action():
    actions = _catalogue_actions()
    # Argo CD actions are open-ended, so the catalogue publishes their prefix only.
    exact = [
        (role, grant)
        for role, grant in _builtin_grants()
        if "*" not in grant and not grant.startswith(ARGO_ACTION_PREFIX)
    ]
    assert exact
    assert [(role, grant) for role, grant in exact if grant not in actions] == []


def test_every_documented_route_action_is_a_scope_constant():
    assert sorted(set(API_ENFORCED_ACTIONS) - set(scopes_dict.values())) == []


@pytest.mark.parametrize("scope", ["transform_compile", "transform_test"])
def test_data_analyst_compiles_and_tests_transforms(scope):
    auth = AuthContext(user_id="analyst", roles=["data_analyst"])
    assert authorize(auth, scopes_dict[scope]).allowed


@pytest.mark.parametrize("scope", ["transform_compile", "transform_test"])
def test_data_analyst_viewer_does_not_compile_or_test_transforms(scope):
    auth = AuthContext(user_id="analyst", roles=["data_analyst_viewer"])
    assert not authorize(auth, scopes_dict[scope]).allowed


def test_infra_viewer_reads_service_surfaces():
    config = RoleConfig.load_builtin()
    assert config.has_permission("infra_viewer", scopes_dict["service_surface_read"])
    assert not config.has_permission("infra_viewer", scopes_dict["service_surface_write"])
