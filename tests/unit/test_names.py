"""Every create request takes its name rule from one place, so no two kinds drift apart.

Accounts, groups, API keys, roles and OIDC providers share the store-name rule. Rules
and hunts share the detection-name rule. Both cap a name at 128 characters, and both
are published in the OpenAPI schema.
"""

import re

import pytest
from pydantic import BaseModel

from dfe_engine.api.v1.account_groups import CreateGroupRequest
from dfe_engine.api.v1.accounts import CreateAccountRequest
from dfe_engine.api.v1.api_keys import CreateAPIKeyRequest
from dfe_engine.api.v1.hunts import HuntCreateRequest
from dfe_engine.api.v1.oidc_providers import CreateProviderRequest
from dfe_engine.api.v1.roles import CreateRoleRequest
from dfe_engine.api.v1.rules import RuleCreateRequest
from dfe_engine.auth import store_names
from dfe_engine.names import (
    InvalidNameError,
    validate_hunt_name,
    validate_rule_name,
    validate_store_name,
)

_PASSWORD = "a-password-long-enough"

_STORE_PATTERN = "^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$"
_DETECTION_PATTERN = "^[A-Za-z0-9_-]{1,128}$"

# (request model, its name field, the published pattern)
_PUBLISHED: list[tuple[type[BaseModel], str, str]] = [
    (CreateAccountRequest, "username", _STORE_PATTERN),
    (CreateAPIKeyRequest, "name", _STORE_PATTERN),
    (CreateGroupRequest, "name", _STORE_PATTERN),
    (CreateRoleRequest, "name", _STORE_PATTERN),
    (CreateProviderRequest, "name", _STORE_PATTERN),
    (RuleCreateRequest, "name", _DETECTION_PATTERN),
    (HuntCreateRequest, "name", _DETECTION_PATTERN),
]

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
def test_every_store_object_is_created_under_exactly_the_store_names(name, accepted):
    provider = {"type": "generic"}

    assert _accepted(lambda: validate_store_name(name)) is accepted
    assert _accepted(lambda: CreateRoleRequest(name=name, permissions=[])) is accepted
    assert _accepted(lambda: CreateGroupRequest(name=name, roles=[])) is accepted
    assert _accepted(lambda: CreateAccountRequest(username=name, password=_PASSWORD)) is accepted
    assert _accepted(lambda: CreateAPIKeyRequest(name=name)) is accepted
    assert _accepted(lambda: CreateProviderRequest(name=name, **provider)) is accepted


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


@pytest.mark.parametrize(("model", "field", "pattern"), _PUBLISHED)
def test_every_create_request_publishes_its_name_rule(model, field, pattern):
    """A client builds its form from the spec, so the rule is in the request schema."""
    published = model.model_json_schema()["properties"][field]

    assert (published["type"], published["pattern"], published["maxLength"]) == (
        "string",
        pattern,
        128,
    )


@pytest.mark.parametrize(
    ("pattern", "names"), [(_STORE_PATTERN, _STORE), (_DETECTION_PATTERN, _DETECTION)]
)
def test_a_published_pattern_accepts_exactly_what_the_validator_accepts(pattern, names):
    """ECMAScript's ``$`` without the multiline flag ends the string, as ``fullmatch`` does."""
    body = pattern.removeprefix("^").removesuffix("$")

    assert {name for name, _ in names if re.fullmatch(body, name)} == {
        name for name, accepted in names if accepted
    }


def test_the_store_key_module_hands_out_no_name_rule():
    """dfe_engine.names is where a name rule comes from; store_names keeps only the store key."""
    assert not hasattr(store_names, "VALID_NAME")
