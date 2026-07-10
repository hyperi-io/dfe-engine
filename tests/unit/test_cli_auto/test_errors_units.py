#  Project:      dfe-engine
#  File:         tests/unit/test_cli_auto/test_errors_units.py
#  Purpose:      Exit-code taxonomy + ErrorResponse rendering (errors.py)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Pure-unit coverage of ``errors.py`` - the CLI's error -> (line, exit-code) map.

Real ``httpx.Response`` / ``httpx.HTTPStatusError`` objects are constructed (not
mocked) so the status -> exit-code taxonomy and the several ``ErrorResponse`` body
shapes (bare ``{code,message}``, FastAPI ``{detail:{...}}`` / ``{detail:"..."}``,
the 422 validation list, and a non-JSON body) are pinned exactly. ``test_errors_help``
covers the 404 path end to end; this fills in every other branch.
"""

from __future__ import annotations

import io

import httpx
import pytest

from dfe_engine.cli.auto.errors import (
    DfeCliError,
    DfeConfigError,
    ExitCode,
    cli_message,
    from_http_status_error,
    handle,
)


def _status_error(status: int, **kwargs) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://engine.test/api/v1/x")
    response = httpx.Response(status, request=request, **kwargs)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


# --- status -> exit-code taxonomy --------------------------------------------


def test_422_maps_to_validation_and_joins_detail_list():
    exc = _status_error(422, json={"detail": [{"loc": ["body", "name"], "msg": "field required"}]})
    cli = from_http_status_error(exc)
    assert cli.exit_code == int(ExitCode.VALIDATION)
    assert "name: field required" in cli.message


def test_4xx_maps_to_api_with_fastapi_detail_dict():
    exc = _status_error(404, json={"detail": {"code": "not_found", "message": "no such org"}})
    cli = from_http_status_error(exc)
    assert cli.exit_code == int(ExitCode.API)
    assert cli.message == "Error (not_found): no such org"


def test_bare_error_response_body():
    # An ErrorResponse with no `detail` wrapper: {code, message} at the top level.
    exc = _status_error(500, json={"code": "boom", "message": "kaboom"})
    cli = from_http_status_error(exc)
    assert cli.exit_code == int(ExitCode.API)
    assert cli.message == "Error (boom): kaboom"


def test_detail_string_body():
    exc = _status_error(400, json={"detail": "bad request bits"})
    cli = from_http_status_error(exc)
    assert cli.exit_code == int(ExitCode.API)
    assert cli.message == "Error (400): bad request bits"


def test_non_json_body_falls_back_to_reason_phrase():
    exc = _status_error(502, content=b"<html>upstream</html>")
    cli = from_http_status_error(exc)
    assert cli.exit_code == int(ExitCode.API)
    assert cli.message.startswith("Error (502):")


def test_3xx_status_is_general():
    # raise_for_status never yields a 3xx, but the mapper must still classify one.
    exc = _status_error(302)
    cli = from_http_status_error(exc)
    assert cli.exit_code == int(ExitCode.GENERAL)


# --- handle() dispatch -------------------------------------------------------


def test_handle_status_error_writes_and_returns_code():
    exc = _status_error(404, json={"detail": {"code": "not_found", "message": "gone"}})
    sink: list[str] = []
    code = handle(exc, emit=sink.append)
    assert code == int(ExitCode.API)
    assert sink == ["Error (not_found): gone"]


def test_handle_config_error_prefixes_and_uses_config_code():
    sink: list[str] = []
    code = handle(DfeConfigError("No engine URL configured"), emit=sink.append)
    assert code == int(ExitCode.CONFIG)
    assert sink == ["Error: No engine URL configured"]


def test_handle_connect_error_is_api():
    sink: list[str] = []
    code = handle(httpx.ConnectError("connection refused"), emit=sink.append)
    assert code == int(ExitCode.API)
    assert sink[0].startswith("Error (connection):")


def test_handle_other_http_error_is_api():
    sink: list[str] = []
    code = handle(httpx.ReadTimeout("slow"), emit=sink.append)
    assert code == int(ExitCode.API)
    assert sink[0].startswith("Error (http):")


def test_handle_generic_exception_is_general():
    sink: list[str] = []
    code = handle(ValueError("weird"), emit=sink.append)
    assert code == int(ExitCode.GENERAL)
    assert sink == ["Error: weird"]


def test_handle_debug_reraises():
    with pytest.raises(ValueError):
        handle(ValueError("boom"), debug=True)


# --- helpers -----------------------------------------------------------------


def test_cli_message_prefixes_only_when_needed():
    assert cli_message(DfeCliError("Error (x): y")) == "Error (x): y"
    assert cli_message(DfeCliError("plain message")) == "Error: plain message"


def test_dfe_cli_error_show_has_no_extra_prefix():
    buf = io.StringIO()
    DfeCliError("just the message").show(file=buf)
    assert buf.getvalue().strip() == "just the message"
