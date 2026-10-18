#  Project:      dfe-engine
#  File:         http_errors.py
#  Purpose:      Describe a failed remote API call without the URL or the user it names
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""What a failed call to a remote API may say in a log line or an API answer.

The identity providers, HyperDX and the secret store all fail this way. A client's
exception text carries the request URL, and the URL names the user: the Google
Directory API's ``userKey=<email>``, Graph's ``/users/<id>``, Okta's
``/users/<id or login>``. The response body can echo the user as well. So a failure
is described by the exception class, the HTTP status and the provider's own error
code, never by its text. A call that got no answer says why by the class of the
socket error under the client's own (DNS, TLS, refused, timed out). The scalo log
scrubber does not cover this: it redacts a literal email, but not a percent-encoded
one or an opaque user id.
"""

import json
import re
import secrets
import socket
import ssl
import traceback
from pathlib import Path

# A provider error code is a short token; anything longer in a code slot may be prose naming the user.
# ``\Z``, not ``$``: ``$`` also matches before a trailing newline.
_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}\Z")

# The error codes RFC 6749 sections 4.1.2.1 and 5.2 and OpenID Connect Core 1.0 section 3.1.2.6 define.
OAUTH_ERROR_CODES = frozenset(
    {
        "access_denied",
        "account_selection_required",
        "consent_required",
        "interaction_required",
        "invalid_client",
        "invalid_grant",
        "invalid_request",
        "invalid_request_object",
        "invalid_request_uri",
        "invalid_scope",
        "login_required",
        "registration_not_supported",
        "request_not_supported",
        "request_uri_not_supported",
        "server_error",
        "temporarily_unavailable",
        "unauthorized_client",
        "unsupported_grant_type",
        "unsupported_response_type",
    }
)

UNRECOGNISED_OAUTH_ERROR = "unrecognised"
"""The code recorded for an OAuth error code the standards do not define."""


def _google_error_status(exc: BaseException) -> tuple[int | None, object]:
    """The status and parsed body of a Google client's error, else ``(None, None)``.

    A googleapiclient ``HttpError`` carries both. A google-auth ``RefreshError`` has no
    status and keeps the token endpoint's parsed answer as its second argument.
    """
    try:
        from google.auth.exceptions import RefreshError  # type: ignore[import-untyped]
        from googleapiclient.errors import HttpError  # type: ignore[import-untyped]
    except ImportError:
        return None, None
    if isinstance(exc, RefreshError):
        return None, exc.args[1] if len(exc.args) > 1 else None
    if not isinstance(exc, HttpError):
        return None, None
    try:
        return exc.status_code, json.loads(exc.content)
    except ValueError:
        return exc.status_code, None


def _is_google_token_refusal(exc: BaseException) -> bool:
    """Whether Google's token endpoint refused the service account, rather than failing itself.

    google-auth raises ``RefreshError`` for every answer but 200, and marks the ones a
    retry may clear (a 5xx, ``temporarily_unavailable``) ``retryable``. Those are the
    provider failing, not the credential being refused.
    """
    try:
        from google.auth.exceptions import RefreshError  # type: ignore[import-untyped]
    except ImportError:
        return False
    return isinstance(exc, RefreshError) and not exc.retryable


def _recognised_oauth_error(exc: BaseException, code: str) -> str:
    """``code`` when a standard defines it or the library set it, else :data:`UNRECOGNISED_OAUTH_ERROR`.

    Authlib copies a callback's ``error`` query parameter into the error it raises,
    so whoever calls the callback chooses that value. A code the class itself
    carries, such as ``mismatching_state``, is the library's own.
    """
    if code in OAUTH_ERROR_CODES or code == getattr(type(exc), "error", None):
        return code
    return UNRECOGNISED_OAUTH_ERROR


def _status_and_body(exc: BaseException) -> tuple[int | None, object]:
    """The HTTP status and parsed JSON body of the provider's answer, when ``exc`` carries one.

    An Authlib error has no response, only the OAuth ``error`` code, and that code is
    all it yields, held to the codes the standards define: a callback's ``error`` and
    ``error_description`` query parameters land in it from whoever called, so its
    description never leaves this module.
    """
    # Matched by shape: httpx, Authlib's httpx2 and requests each raise their own class.
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if response is not None and isinstance(status, int):
        try:
            return status, response.json()
        except ValueError:
            return status, None
    oauth_error = getattr(exc, "error", None)
    if isinstance(oauth_error, str):
        return None, {"error": _recognised_oauth_error(exc, oauth_error)}
    return _google_error_status(exc)


def _transport_failure(exc: BaseException) -> str:
    """Why a call that got no answer failed, else empty.

    One of ``dns``, ``tls``, ``connection_refused``, ``connection_reset`` or ``timeout``.
    The HTTP client wraps the socket's error in its own, so the class that says which
    is somewhere down the cause chain. It is read from the classes there, never from
    any message, because the message names the host.
    """
    seen: set[int] = set()
    link: BaseException | None = exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        if isinstance(link, ssl.SSLError):
            return "tls"
        if isinstance(link, socket.gaierror):
            return "dns"
        if isinstance(link, ConnectionRefusedError):
            return "connection_refused"
        # A reset is not an SSLError: mid-handshake it points at a TLS mismatch or a middlebox, so it gets its own class.
        if isinstance(link, ConnectionResetError):
            return "connection_reset"
        # httpx and httpcore name their timeouts TimeoutException; the pool timeout has no socket error under it.
        if isinstance(link, TimeoutError) or any(
            cls.__name__ == "TimeoutException" for cls in type(link).__mro__
        ):
            return "timeout"
        link = link.__cause__ or link.__context__
    return ""


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


def describe_http_error(exc: BaseException) -> dict[str, object]:
    """The log fields for a failed remote API call: never its text, its URL or its response body.

    Args:
        exc: The failure the API client raised.

    Returns:
        ``error_type`` always, ``status`` when the provider answered, ``code`` when
        its answer carried an error code, and ``transport_failure`` (``dns``, ``tls``,
        ``connection_refused``, ``connection_reset`` or ``timeout``) when the call never
        got an answer.
    """
    fields: dict[str, object] = {"error_type": type(exc).__name__}
    status, body = _status_and_body(exc)
    if status is not None:
        fields["status"] = status
    if code := provider_error_code(body):
        fields["code"] = code
    if transport_failure := _transport_failure(exc):
        fields["transport_failure"] = transport_failure
    return fields


def _got_no_answer(exc: BaseException) -> bool:
    """Whether the call failed before any answer arrived: a socket, TLS or timeout failure.

    Read from the classes down the cause chain. httpx and httpcore name theirs
    ``TransportError``, and requests' are ``OSError`` subclasses. A ``ValueError`` is a
    body that did not parse, so an answer, even where requests makes it an ``OSError``.
    """
    seen: set[int] = set()
    link: BaseException | None = exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        transport = any(cls.__name__ == "TransportError" for cls in type(link).__mro__)
        if transport or (isinstance(link, OSError) and not isinstance(link, ValueError)):
            return True
        link = link.__cause__ or link.__context__
    return False


def is_outage(exc: BaseException) -> bool:
    """Whether a failed call means the provider could not answer, rather than that it declined.

    An outage is a call that got no answer, a 429, a 5xx, or a Google token refresh
    google-auth marks retryable. Every other failure is the provider answering: a 401 or
    403, a missing scope, any other 4xx, or a body that is not what the call reads.

    Args:
        exc: The failure the API client raised.

    Returns:
        True for an outage.
    """
    status, _body = _status_and_body(exc)
    if status is not None:
        return status == 429 or status >= 500
    return _is_google_retryable(exc) or _got_no_answer(exc)


def _is_google_retryable(exc: BaseException) -> bool:
    """Whether google-auth marks a token refresh as one a retry may clear: Google failing, not refusing."""
    try:
        from google.auth.exceptions import RefreshError  # type: ignore[import-untyped]
    except ImportError:
        return False
    return isinstance(exc, RefreshError) and bool(exc.retryable)


def failure_frames(exc: BaseException, *, limit: int = 10) -> list[str]:
    """Where ``exc`` was raised, innermost last, as ``file.py:line function``.

    The file's name alone, with no directory, source line or local, so a frame names
    code and never data.

    Args:
        exc: The failure.
        limit: Frames kept, counted from the innermost.

    Returns:
        The frames, empty when ``exc`` was never raised.
    """
    frames = traceback.extract_tb(exc.__traceback__)[-limit:]
    return [f"{Path(frame.filename).name}:{frame.lineno} {frame.name}" for frame in frames]


def describe_failure(exc: BaseException) -> dict[str, object]:
    """:func:`describe_http_error`'s fields plus :func:`failure_frames`, for a failure in our own code.

    Args:
        exc: The failure.

    Returns:
        The log fields, never the failure's text.
    """
    return {**describe_http_error(exc), "frames": failure_frames(exc)}


def failure_reference() -> str:
    """A fresh id joining one failure's generic answer to the log line holding its reason."""
    return secrets.token_hex(6)


def referenced(message: str, reference: str) -> str:
    """``message`` naming ``reference``, so whoever reads the answer can find its log line."""
    return f"{message} (reference {reference})"


def credential_failure_message(*, service: str) -> str:
    """What an API caller is told when ``service`` gives no access to the configured credential."""
    return (
        f"{service} could not use the configured credential: check it is current "
        "and valid; the engine log has the reason"
    )


def http_failure_message(exc: BaseException, *, service: str) -> str:
    """What an API caller is told about a failed remote API call: its status class, never the provider's text.

    Args:
        exc: The failure the API client raised.
        service: The API the call went to, as the caller knows it.

    Returns:
        A message naming ``service`` and the HTTP status, pointing at the engine log.
        A Google token refusal has no status, and its message says the configured
        credential gave no access. Any other failure with no status says the service
        could not be reached only when a socket error under it says so, because an
        answer that arrived but could not be used is not an unreachable service. A
        429 says the service is throttling, never that it rejected the request.
    """
    if _is_google_token_refusal(exc):
        return credential_failure_message(service=service)
    status, _body = _status_and_body(exc)
    if status is None:
        if _transport_failure(exc):
            return f"{service} could not be reached; the engine log has the reason"
        return f"{service} gave no usable answer; the engine log has the reason"
    if status in (401, 403):
        return (
            f"{service} refused the request (HTTP {status}): check the credential and the "
            "permissions granted to it; the engine log has the provider's error code"
        )
    if status == 429:
        return f"{service} is throttling requests (HTTP 429); retry shortly"
    if status >= 500:
        return f"{service} failed on the provider's side (HTTP {status}); retry shortly"
    return f"{service} rejected the request (HTTP {status}); the engine log has the provider's error code"
