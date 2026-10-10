#  Project:      dfe-engine
#  File:         names.py
#  Purpose:      What each kind of named object may be called
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""What each kind of named object may be called.

Every name here becomes a file stem (``{name}.yaml``), a document key or a URL path
segment, so each rule refuses a path separator, refuses a trailing newline (it anchors
at the end of the string, not at ``$``) and caps the length at :data:`MAX_NAME_LENGTH`.
Create requests take the annotated types, and the account, group and API key stores
check :data:`VALID_NAME`, so a name the request accepts is one the store can hold. Each
annotated type publishes its rule as a ``pattern`` and ``maxLength`` in the OpenAPI
schema, so a client can check a name before it sends one.
"""

import re
from typing import Annotated

from pydantic import AfterValidator, WithJsonSchema

MAX_NAME_LENGTH = 128
"""The longest name an account, group, API key, role, rule or hunt may have."""

VALID_NAME = re.compile(rf"^[a-zA-Z0-9][a-zA-Z0-9._-]{{0,{MAX_NAME_LENGTH - 1}}}\Z")
"""An account, group, API key or role name.

The leading letter or digit keeps ``.``, ``..`` and ``-x`` out of a store directory.
"""

DETECTION_NAME = re.compile(rf"^[A-Za-z0-9_-]{{1,{MAX_NAME_LENGTH}}}\Z")
"""A rule or hunt name.

No ``.``: this is the charset of every rule and hunt id the engine generates (sigma
bindings, HyperDX views).
"""


class InvalidNameError(ValueError):
    """A name that breaks the rule for its kind of object."""


def validate_store_name(name: str) -> str:
    """Return *name* when an account, group, API key or role can be stored under it.

    Args:
        name: The name, often straight off a request.

    Returns:
        *name*, unchanged.

    Raises:
        InvalidNameError: *name* does not match :data:`VALID_NAME`.
    """
    if not VALID_NAME.match(name):
        raise InvalidNameError(
            f"name {name!r} must start with a letter or digit and hold only letters, "
            f"digits, '.', '_' or '-', at most {MAX_NAME_LENGTH} characters"
        )
    return name


def _detection_name(name: str, kind: str) -> str:
    if not DETECTION_NAME.match(name):
        raise InvalidNameError(
            f"{kind} name {name!r} must be letters, digits, '_' or '-', "
            f"at most {MAX_NAME_LENGTH} characters"
        )
    return name


def validate_rule_name(name: str) -> str:
    """Return *name* when a rule can be created, or newly referenced by a hunt, under it.

    Args:
        name: The rule name.

    Returns:
        *name*, unchanged.

    Raises:
        InvalidNameError: *name* does not match :data:`DETECTION_NAME`.
    """
    return _detection_name(name, "rule")


def validate_hunt_name(name: str) -> str:
    """Return *name* when a hunt can be created under it.

    Args:
        name: The hunt name.

    Returns:
        *name*, unchanged.

    Raises:
        InvalidNameError: *name* does not match :data:`DETECTION_NAME`.
    """
    return _detection_name(name, "hunt")


def _published(rule: re.Pattern[str]) -> WithJsonSchema:
    r"""The OpenAPI schema for *rule*, which validates nothing itself.

    ECMAScript has no ``\Z``, and its ``$`` without the multiline flag ends the string.
    """
    return WithJsonSchema(
        {
            "type": "string",
            "pattern": rule.pattern.removesuffix(r"\Z") + "$",
            "maxLength": MAX_NAME_LENGTH,
        }
    )


StoreName = Annotated[str, AfterValidator(validate_store_name), _published(VALID_NAME)]
"""A request field holding an account, group, API key or role name."""

RuleName = Annotated[str, AfterValidator(validate_rule_name), _published(DETECTION_NAME)]
"""A request field holding the name a rule is created under."""

HuntName = Annotated[str, AfterValidator(validate_hunt_name), _published(DETECTION_NAME)]
"""A request field holding the name a hunt is created under."""
