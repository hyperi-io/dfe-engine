#  Project:      dfe-engine
#  File:         cli/auto/client.py
#  Purpose:      Sync httpx client that turns an Operation + options into a call
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Thin sync HTTP client the generated commands call.

Outbound HTTP goes through ``scalo.http.HttpClient`` (the scalo-first rule - one
retry / backoff / breaker / traceparent policy), never a raw ``httpx`` client. The
no-mock tests inject a real in-process ASGI session (a Starlette ``TestClient``,
itself a sync httpx client over the live app) through the ``session`` seam, so the
CLI still exercises the actual engine end-to-end without a mock. Both the scalo
client and the TestClient expose the same ``get/post/put/patch/delete -> Response``
duck type, so the dispatch is identical either way.

Auth header: an api_key credential -> ``X-API-Key``; a token credential ->
``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx
from scalo.http import HttpClient

from .config import Credential
from .errors import DfeCliError, ExitCode
from .spec import Operation

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_RETRIES = 3


def _parse_json(raw: str, name: str) -> Any:
    """Parse a JSON option value, mapping a decode error to a friendly CLI error."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DfeCliError(f"invalid JSON for {name}: {exc}", ExitCode.VALIDATION) from exc


class Client:
    """Binds one base URL + credential to a scalo HTTP session (or an injected one)."""

    def __init__(
        self,
        base_url: str,
        credential: Credential | None = None,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        retries: int = _DEFAULT_RETRIES,
        session: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.credential = credential
        if session is not None:
            # Injected requester - the no-mock tests pass a real in-process ASGI
            # session (Starlette TestClient) exposing get/post/... -> Response.
            self._session = session
            self._owns_session = False
        else:
            # scalo-first: retry / backoff / breaker / traceparent in one place.
            # ``retries`` is 0 for non-idempotent verbs (POST/PUT/DELETE) so a
            # retried request can never double-mutate; GETs keep the default.
            self._session = HttpClient(base_url=self.base_url, timeout=timeout, retries=retries)
            self._owns_session = True

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- header ---------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        cred = self.credential
        if cred is None or cred.value is None:
            return {}
        if cred.is_api_key:
            return {"X-API-Key": cred.value}
        return {"Authorization": f"Bearer {cred.value}"}

    # -- request building -----------------------------------------------------

    def build_url(self, op: Operation, path_args: dict[str, Any]) -> str:
        """Substitute path params into the operation template."""
        path = op.path
        for name, value in path_args.items():
            path = path.replace("{" + name + "}", quote(str(value), safe=""))
        return path

    def build_query(self, op: Operation, query_args: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {}
        for param in op.query_params:
            if param.name in query_args and query_args[param.name] is not None:
                params[param.name] = query_args[param.name]
        return params

    def build_body(self, op: Operation, body_args: dict[str, Any]) -> Any:
        """Assemble the JSON request body from the body-prop options.

        Object/array props whose value arrived as a JSON string are parsed; a
        freeform body op takes the raw ``--body`` JSON string as the whole body.
        A malformed JSON string raises a friendly ``DfeCliError`` (param-error
        exit code) rather than surfacing a raw ``JSONDecodeError`` (exit 255).
        """
        if op.freeform_body:
            raw = body_args.get("body")
            if raw is None:
                return None
            if isinstance(raw, str):
                return _parse_json(raw, "body")
            return raw

        body: dict[str, Any] = {}
        for prop in op.body_props:
            if prop.name not in body_args:
                continue
            value = body_args[prop.name]
            if value is None:
                continue
            if prop.type == "object" and isinstance(value, str):
                value = _parse_json(value, prop.name)
            elif prop.type == "array" and isinstance(value, tuple):
                value = list(value)
            body[prop.name] = value
        return body or None

    def call(
        self,
        op: Operation,
        *,
        path_args: dict[str, Any] | None = None,
        query_args: dict[str, Any] | None = None,
        body_args: dict[str, Any] | None = None,
        query_override: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Execute one operation and return the raw response (raises on 4xx/5xx)."""
        url = self.build_url(op, path_args or {})
        params = self.build_query(op, query_args or {})
        if query_override:
            params.update(query_override)
        body = self.build_body(op, body_args or {})
        headers = self._auth_headers()

        method = op.method.lower()
        request_kwargs: dict[str, Any] = {"params": params, "headers": headers}
        if body is not None and method in ("post", "put", "patch", "delete"):
            request_kwargs["json"] = body

        # scalo.http exposes get/post/put/patch/delete (no generic request()).
        send = getattr(self._session, method)
        response = send(url, **request_kwargs)
        # scalo already raises on non-2xx; an injected raw session (TestClient)
        # returns it, so keep this - a no-op after a scalo success.
        response.raise_for_status()
        return response

    def raw(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Low-level call for the built-in auth endpoints (login/me) that the CLI
        drives directly - they are ``x-cli`` hidden, so they are not in the
        generated command tree. Same session dispatch + raise as ``call``.
        """
        send = getattr(self._session, method.lower())
        response = send(path, **kwargs)
        response.raise_for_status()
        return response

    def call_json(self, op: Operation, **kwargs: Any) -> Any:
        """Execute and parse the JSON body (None for 204/empty)."""
        response = self.call(op, **kwargs)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text
