#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/conftest.py
#  Purpose:      Fixtures for OIDC unit tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Fixtures for OIDC unit tests."""

from __future__ import annotations

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
