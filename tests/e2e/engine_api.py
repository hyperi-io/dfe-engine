#  Project:      dfe-engine
#  File:         tests/e2e/engine_api.py
#  Purpose:      The engine API client every live e2e suite drives the control plane with
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One authenticated client for the live engine API.

A plain module rather than a conftest: two live suites drive the control plane
now, and a second copy of this would be a second place for the token handling or
the TLS posture to drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class EngineAPI:
    """The engine's API, with a token this suite keeps fresh.

    A live run waits minutes per case, so it outlives the token it started with;
    every call re-logs in once on a 401 rather than the run dying part-way
    through.
    """

    base: str
    user: str
    password: str
    verify: bool
    token: str | None = None

    def _client(self) -> httpx.Client:
        return httpx.Client(verify=self.verify, timeout=120.0)

    def login(self) -> str:
        with self._client() as client:
            response = client.post(
                f"{self.base}/api/v1/auth/login",
                json={"username": self.user, "password": self.password},
            )
        assert response.status_code == 200, f"engine login failed: {response.text}"
        self.token = str(response.json()["access_token"])
        return self.token

    def call(self, method: str, path: str, body: Any = None) -> httpx.Response:
        """One API call, returning the response whatever its status.

        A status is the assertion in several cases here - a refused save is the
        point of the archived shape - so this never raises on one.
        """
        if self.token is None:
            self.login()
        url = f"{self.base}/api/v1{path}"
        response = self._request(method, url, body)
        if response.status_code == 401:
            self.login()
            response = self._request(method, url, body)
        return response

    def _request(self, method: str, url: str, body: Any) -> httpx.Response:
        with self._client() as client:
            return client.request(
                method, url, json=body, headers={"Authorization": f"Bearer {self.token}"}
            )

    def json(self, method: str, path: str, body: Any = None) -> Any:
        """One API call that must succeed, decoded."""
        response = self.call(method, path, body)
        assert response.status_code < 300, (
            f"{method} {path} -> {response.status_code}: {response.text}"
        )
        return response.json() if response.content else None
