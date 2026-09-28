#  Project:      dfe-engine
#  File:         tests/unit/test_e2e_harness/test_secret_repr.py
#  Purpose:      Assert the live suites' config objects never print a secret
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""pytest prints a fixture object in a failure trace, so no secret may reach its repr."""

import dataclasses

import pytest

from tests.e2e.conftest import E2EConfig, OIDCFixtureLogin
from tests.e2e.engine_api import EngineAPI

_SECRET = "s3cret-value-that-must-not-print"
_SECRET_FIELDS = {
    E2EConfig: {
        "receiver_token",
        "ch_password",
        "hyperdx_api_key",
        "engine_token",
        "engine_password",
        "engine_new_password",
        "deploy_repo_token",
    },
    OIDCFixtureLogin: {"password"},
    EngineAPI: {"password", "token", "new_password"},
}


def _build(cls: type) -> object:
    """Fill every field: secrets with the marker, the rest with a harmless value."""
    values: dict[str, object] = {}
    for spec in dataclasses.fields(cls):
        if spec.name in _SECRET_FIELDS[cls]:
            values[spec.name] = _SECRET
        elif spec.type in ("int", int):
            values[spec.name] = 1
        elif spec.type in ("bool", bool):
            values[spec.name] = False
        else:
            values[spec.name] = "visible"
    return cls(**values)


@pytest.mark.parametrize("cls", list(_SECRET_FIELDS), ids=lambda cls: cls.__name__)
def test_no_secret_reaches_the_repr(cls: type) -> None:
    rendered = repr(_build(cls))
    assert _SECRET not in rendered
    assert "visible" in rendered


@pytest.mark.parametrize("cls", list(_SECRET_FIELDS), ids=lambda cls: cls.__name__)
def test_every_secret_looking_field_is_hidden(cls: type) -> None:
    """A new token or password field that forgets repr=False fails here."""
    looks_secret = {
        spec.name
        for spec in dataclasses.fields(cls)
        if any(word in spec.name for word in ("password", "token", "api_key", "secret"))
    }
    shown = {spec.name for spec in dataclasses.fields(cls) if spec.repr}
    assert not (looks_secret & shown)
