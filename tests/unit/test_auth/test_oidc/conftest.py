#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/conftest.py
#  Purpose:      Fixtures for OIDC unit tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Fixtures for OIDC unit tests."""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

from tests.unit.test_auth.factories import make_local_idp
from tests.unit.test_auth.test_oidc.local_idp import LocalIdp


@pytest.fixture
def local_idp() -> Iterator[LocalIdp]:
    """Run a local OIDC provider for the duration of one test."""
    idp = make_local_idp()
    idp.start()
    yield idp
    idp.stop()


@pytest.fixture
def entra_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route every https request through a proxy port nothing listens on, so Entra cannot be reached.

    MSAL pins its authority at login.microsoftonline.com and honours the proxy env, so
    a token request fails on the local socket and never leaves the host.
    """
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    for name in ("https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{port}")
