#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_base_adapter.py
#  Purpose:      Tests for the shared directory adapter helpers
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for the shared directory adapter helpers."""

import re

import pytest

from dfe_engine.auth.oidc.adapters.base import (
    DirectoryError,
    describe_error,
    error_frames,
    fetch_directory_page,
)
from tests.unit.test_auth.factories import make_http_client
from tests.unit.test_auth.test_oidc.base_adapter_cases import (
    DESCRIBE_ERROR_CASES,
    DescribeErrorCase,
    read_message,
)
from tests.unit.test_auth.test_oidc.local_directory import LocalDirectory


class TestDescribeError:
    @pytest.mark.parametrize(
        "case", DESCRIBE_ERROR_CASES, ids=[case["id"] for case in DESCRIBE_ERROR_CASES]
    )
    def test_matches_expected(self, case: DescribeErrorCase):
        description = describe_error(exc=case["exc"], read_summary=case["read_summary"])
        assert description == case["expected_description"]


class TestErrorFrames:
    def test_carries_neither_the_text_nor_the_locals(self):
        def fail(*, token: str) -> None:
            raise ValueError(token)

        # Only the lines a frame stopped at are kept, so the value is set on a line the traceback skips.
        token = "secret-value-7f3a"
        try:
            fail(token=token)
        except ValueError as exc:
            frames = error_frames(exc=exc)
        assert ("secret-value-7f3a" in frames, "raise ValueError(token)" in frames) == (False, True)


class TestFetchDirectoryPage:
    async def test_a_header_the_client_cannot_encode(self, http_directory: LocalDirectory):
        message = "entra_id directory: GET '/v1.0/groups' failed: UnicodeEncodeError"
        async with make_http_client() as client:
            with pytest.raises(DirectoryError, match=f"^{re.escape(message)}$"):
                await fetch_directory_page(
                    client=client,
                    headers={"Authorization": "Bearer ﻿secret-value-7f3a"},
                    provider_type="entra_id",
                    read_summary=read_message,
                    url=f"{http_directory.base_url}/v1.0/groups",
                )
