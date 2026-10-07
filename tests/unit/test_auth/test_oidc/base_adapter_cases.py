#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/base_adapter_cases.py
#  Purpose:      Case tables for the shared directory adapter helpers
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the shared directory adapter helpers."""

import json
from collections.abc import Callable
from typing import Any, TypedDict

from tests.unit.test_auth.factories import make_http_status_error

_SUMMARY_200 = "Insufficient privileges " + "x" * 176


def read_message(body: Any) -> str:
    """The ``message`` field of a JSON error body; empty when there is none."""
    message = body.get("message") if isinstance(body, dict) else None
    return message if isinstance(message, str) else ""


class DescribeErrorCase(TypedDict):
    id: str
    exc: BaseException
    read_summary: Callable[[Any], str] | None
    expected_description: str


DESCRIBE_ERROR_CASES: list[DescribeErrorCase] = [
    {
        "id": "summary_cut_to_200_characters",
        "exc": make_http_status_error(
            body=json.dumps({"message": _SUMMARY_200 + "y" * 50}), status=403
        ),
        "read_summary": read_message,
        "expected_description": f"HTTP 403: {_SUMMARY_200!r}",
    },
    {
        "id": "no_summary_reader",
        "exc": make_http_status_error(body=json.dumps({"message": "Refused"}), status=500),
        "read_summary": None,
        "expected_description": "HTTP 500",
    },
    {
        "id": "body_not_json",
        "exc": make_http_status_error(body="<html>bad gateway</html>", status=502),
        "read_summary": read_message,
        "expected_description": "HTTP 502",
    },
    {
        "id": "not_an_http_error",
        "exc": UnicodeEncodeError(
            "ascii", "Bearer ﻿secret-value-7f3a", 7, 8, "ordinal not in range(128)"
        ),
        "read_summary": None,
        "expected_description": "UnicodeEncodeError",
    },
]
