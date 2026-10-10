"""Every create request takes its name rule from one place, so no two kinds drift apart.

Accounts, groups, API keys and roles share the store-name rule. Rules and hunts share
the detection-name rule. Both cap a name at 128 characters.
"""

import pytest

from dfe_engine.api.v1.account_groups import CreateGroupRequest
from dfe_engine.api.v1.hunts import HuntCreateRequest
from dfe_engine.api.v1.roles import CreateRoleRequest
from dfe_engine.api.v1.rules import RuleCreateRequest
from dfe_engine.names import (
    InvalidNameError,
    validate_hunt_name,
    validate_rule_name,
    validate_store_name,
)

# (name, a store name, a rule or hunt name)
_NAMES = [
    ("certutil", True, True),
    ("win_cert-01", True, True),
    ("role-1.x", True, False),
    ("win.cert", True, False),
    ("_ops", False, True),
    ("-x", False, True),
    (".hidden", False, False),
    ("..", False, False),
    ("a/b", False, False),
    ("../x", False, False),
    ("", False, False),
    ("line\n", False, False),
    ("caf" + chr(0xE9), False, False),
    ("has space", False, False),
    ("a" * 128, True, True),
    ("a" * 129, False, False),
]
_STORE = [(name, store) for name, store, _ in _NAMES]
_DETECTION = [(name, detection) for name, _, detection in _NAMES]


def _accepted(check) -> bool:
    try:
        check()
    except ValueError:
        return False
    return True


@pytest.mark.parametrize(("name", "accepted"), _STORE)
def test_a_role_and_a_group_are_created_under_exactly_the_store_names(name, accepted):
    assert _accepted(lambda: validate_store_name(name)) is accepted
    assert _accepted(lambda: CreateRoleRequest(name=name, permissions=[])) is accepted
    assert _accepted(lambda: CreateGroupRequest(name=name, roles=[])) is accepted


@pytest.mark.parametrize(("name", "accepted"), _DETECTION)
def test_a_rule_and_a_hunt_are_created_under_exactly_the_detection_names(name, accepted):
    hunt = {"cron": "*/5 * * * *", "customers": ["org_a"], "rules": ["certutil"]}

    assert _accepted(lambda: validate_rule_name(name)) is accepted
    assert _accepted(lambda: validate_hunt_name(name)) is accepted
    assert _accepted(lambda: RuleCreateRequest(name=name, user_sql="SELECT 1")) is accepted
    assert _accepted(lambda: HuntCreateRequest(name=name, **hunt)) is accepted


@pytest.mark.parametrize(
    ("check", "kind"),
    [
        (validate_store_name, "name"),
        (validate_rule_name, "rule name"),
        (validate_hunt_name, "hunt name"),
    ],
)
def test_a_refusal_names_the_kind_and_the_cap_and_is_a_value_error(check, kind):
    with pytest.raises(InvalidNameError, match=rf"^{kind} 'a/b' .* at most 128 characters$"):
        check("a/b")

    assert issubclass(InvalidNameError, ValueError)
