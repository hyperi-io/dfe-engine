#  Project:      dfe-engine
#  File:         auth/oidc/idp_errors.py
#  Purpose:      Describe an identity provider failure without the URL or the user it names
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""What a failed identity provider call may say in a log line or an API answer.

An IdP client's exception text carries the request URL, and the URL names the
user: the Google Directory API's ``userKey=<email>``, Graph's ``/users/<id>``,
Okta's ``/users/<id or login>``. The response body can echo the user as well. So a
failure is described by the exception class, the HTTP status and the provider's
own error code, never by its text. The scalo log scrubber does not cover this: it
redacts a literal email, but not a percent-encoded one or an opaque user id.
"""

import json
import re

# A provider error code is a short token; anything longer in a code slot may be prose naming the user.
_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def _google_http_error_status(exc: BaseException) -> tuple[int | None, object]:
    """The status and parsed body of a googleapiclient ``HttpError``, else ``(None, None)``."""
    try:
        from googleapiclient.errors import HttpError  # type: ignore[import-untyped]
    except ImportError:
        return None, None
    if not isinstance(exc, HttpError):
        return None, None
    try:
        return exc.status_code, json.loads(exc.content)
    except ValueError:
        return exc.status_code, None


def _status_and_body(exc: BaseException) -> tuple[int | None, object]:
    """The HTTP status and parsed JSON body of the provider's answer, when ``exc`` carries one."""
    # Matched by shape: httpx, Authlib's httpx2 and requests each raise their own class.
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if response is not None and isinstance(status, int):
        try:
            return status, response.json()
        except ValueError:
            return status, None
    return _google_http_error_status(exc)


def provider_error_code(body: object) -> str:
    """The provider's own error codes in a JSON error body, joined by ``;``, or empty.

    Reads Graph's ``error.code``, an OAuth token endpoint's ``error``, Google's
    ``error.status`` and the ``reason`` of each ``error.details`` or ``error.errors``
    item, and Okta's ``errorCode``. A value that is not a short token is dropped.

    Args:
        body: The parsed response body.

    Returns:
        The codes found, in that order, or an empty string.
    """
    if not isinstance(body, dict):
        return ""
    found: list[object] = [body.get("errorCode")]
    error = body.get("error")
    if isinstance(error, dict):
        found += [error.get("code"), error.get("status")]
        for key in ("details", "errors"):
            items = error.get(key)
            if isinstance(items, list):
                found += [item.get("reason") for item in items if isinstance(item, dict)]
    else:
        found.append(error)
    codes = [code for code in found if isinstance(code, str) and _CODE.match(code)]
    return ";".join(dict.fromkeys(codes))


def describe_idp_error(exc: BaseException) -> dict[str, object]:
    """The log fields for a failed IdP call: never its text, its URL or its response body.

    Args:
        exc: The failure the IdP client raised.

    Returns:
        ``error_type`` always, ``status`` when the provider answered, and ``code``
        when its answer carried an error code.
    """
    fields: dict[str, object] = {"error_type": type(exc).__name__}
    status, body = _status_and_body(exc)
    if status is not None:
        fields["status"] = status
    if code := provider_error_code(body):
        fields["code"] = code
    return fields


def idp_failure_message(exc: BaseException, *, service: str) -> str:
    """What an API caller is told about a failed IdP call: its status class, never the provider's text.

    Args:
        exc: The failure the IdP client raised.
        service: The API the call went to, as the caller knows it.

    Returns:
        A message naming ``service`` and the HTTP status, pointing at the engine log.
    """
    status, _body = _status_and_body(exc)
    if status is None:
        return f"{service} could not be reached; the engine log has the reason"
    if status in (401, 403):
        return (
            f"{service} refused the request (HTTP {status}): check the credential and the "
            "permissions granted to it; the engine log has the provider's error code"
        )
    if status >= 500:
        return f"{service} failed on the provider's side (HTTP {status}); retry shortly"
    return f"{service} rejected the request (HTTP {status}); the engine log has the provider's error code"
