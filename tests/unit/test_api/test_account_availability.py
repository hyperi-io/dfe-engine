#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_account_availability.py
#  Purpose:      API tests for whether a new account could take a name and hold an email
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.unit.test_api.account_availability_cases import AVAILABILITY_CASES, AvailabilityCase
from tests.unit.test_auth.factories import make_account

_AVAILABILITY = "/api/v1/auth/accounts/availability"


@pytest.fixture
def stored(app: FastAPI, client: TestClient) -> FastAPI:
    """Store kaz with a vouched email, jo with a self-set one, and okta-kaz linked to kaz."""
    store = app.state.account_store
    store.put(make_account(email="kaz@example.test", email_verified=True, username="kaz"))
    store.put(make_account(email="jo@example.test", email_verified=False, username="jo"))
    store.put(
        make_account(
            external=True,
            linked_to="kaz",
            source_provider="okta",
            subject="00u1",
            username="okta-kaz",
        )
    )
    return app


class TestAvailability:
    @pytest.mark.parametrize(
        "case", AVAILABILITY_CASES, ids=[case["id"] for case in AVAILABILITY_CASES]
    )
    def test_matches_expected(
        self,
        admin_headers: dict[str, str],
        client: TestClient,
        stored: FastAPI,
        case: AvailabilityCase,
    ):
        body = client.get(
            _AVAILABILITY,
            headers=admin_headers,
            params={"email": case["email"], "username": case["username"]},
        ).json()
        assert (body["username"], body["email"]) == (
            case["expected_username"],
            case["expected_email"],
        )

    def test_a_reader_without_account_write_is_refused(
        self, client: TestClient, stored: FastAPI, viewer_headers: dict[str, str]
    ):
        resp = client.get(_AVAILABILITY, headers=viewer_headers, params={"username": "x"})
        assert resp.status_code == 403
