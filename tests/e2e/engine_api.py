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

from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class EngineAPI:
    """The engine's API, with a token this suite keeps fresh.

    A live run waits minutes per case, so it outlives the token it started with;
    every call re-logs in once on a 401 rather than the run dying part-way
    through.

    A fresh deployment's admin is due a forced password change at first login,
    and every other route refuses it until then. ``new_password`` is what the
    client changes it to, after which it signs in with that.
    """

    base: str
    user: str
    # Secrets are repr=False: pytest prints this object in a failure trace.
    password: str = field(repr=False)
    verify: bool
    token: str | None = field(default=None, repr=False)
    new_password: str | None = field(default=None, repr=False)

    def _client(self) -> httpx.Client:
        return httpx.Client(verify=self.verify, timeout=120.0)

    def login(self) -> str:
        response = self._login(self.password)
        if response.status_code == 401 and self.new_password:
            # An earlier suite already replaced the issued password.
            response = self._login(self.new_password)
            self.password = self.new_password
        assert response.status_code == 200, f"engine login failed: {response.text}"
        self.token = str(response.json()["access_token"])
        if response.json().get("password_change_required"):
            self._complete_forced_change()
        return self.token

    def _login(self, password: str) -> httpx.Response:
        with self._client() as client:
            return client.post(
                f"{self.base}/api/v1/auth/login",
                json={"username": self.user, "password": password},
            )

    def _complete_forced_change(self) -> None:
        """Replace the issued password, as the account's owner must before anything else."""
        assert self.new_password, (
            f"'{self.user}' must change its issued password before the engine serves it. "
            "Set DFE_E2E_ADMIN_NEW_PASSWORD to the password to change it to"
        )
        changed = self._request(
            "POST",
            f"{self.base}/api/v1/auth/accounts/reset-password",
            {"new_password": self.new_password},
        )
        assert changed.status_code == 200, f"forced password change failed: {changed.text}"
        self.password = self.new_password

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
