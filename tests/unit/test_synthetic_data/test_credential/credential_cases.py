#  Project:      dfe-engine
#  File:         tests/unit/test_synthetic_data/test_credential/credential_cases.py
#  Purpose:      Cases for the synthetic-data stream credential
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Cases for the synthetic-data stream credential."""

from typing import Any, TypedDict

from dfe_engine.orgs.ingest_credentials import SYNTHETIC_DATA_CREDENTIAL, IngestCredential
from tests.unit.test_synthetic_data.factories import make_ingest_credential

ROTATED_AT = "2026-11-16T00:00:00+00:00"

# What every call leaves stored with acme named primary.
WANTED: dict[str, Any] = {
    "default_org": "acme",
    "listeners": ["http"],
    "name": SYNTHETIC_DATA_CREDENTIAL,
    "org": "acme",
    "orgs": ["acme"],
}


class RefusedUntilCase(TypedDict):
    id: str
    rotated_at: str
    expected_refused_until: float


class StreamCredentialCase(TypedDict):
    id: str
    stored: IngestCredential
    stored_token: str | None
    expected_minted: bool
    expected_old_token: str | None


class StreamCredentialNoneCase(TypedDict):
    id: str
    doc: str


REFUSED_UNTIL_CASES: list[RefusedUntilCase] = [
    {"id": "rotated", "rotated_at": ROTATED_AT, "expected_refused_until": 1794787290.0},
    {"id": "no_rotation_time", "rotated_at": "", "expected_refused_until": 0.0},
]

STREAM_CREDENTIAL_CASES: list[StreamCredentialCase] = [
    {
        "id": "held",
        "stored": make_ingest_credential(
            name=SYNTHETIC_DATA_CREDENTIAL, org="acme", rotated_at=ROTATED_AT, token_id="old"
        ),
        "stored_token": "old-token",
        "expected_minted": False,
        "expected_old_token": "old-token",
    },
    {
        "id": "tokenless",
        "stored": make_ingest_credential(
            name=SYNTHETIC_DATA_CREDENTIAL, org="acme", rotated_at=ROTATED_AT, token_id="old"
        ),
        "stored_token": None,
        "expected_minted": True,
        "expected_old_token": None,
    },
    {
        "id": "another_org",
        "stored": make_ingest_credential(
            name=SYNTHETIC_DATA_CREDENTIAL, org="beta", token_id="old"
        ),
        "stored_token": "old-token",
        "expected_minted": True,
        "expected_old_token": None,
    },
    {
        "id": "another_listener",
        "stored": make_ingest_credential(
            listeners=["otlp"], name=SYNTHETIC_DATA_CREDENTIAL, org="acme", token_id="old"
        ),
        "stored_token": "old-token",
        "expected_minted": True,
        "expected_old_token": None,
    },
]

STREAM_CREDENTIAL_NONE_CASES: list[StreamCredentialNoneCase] = [
    {"id": "no_primary_org", "doc": ""},
    {"id": "unreadable_primary_org", "doc": "- acme\n- beta\n"},
]
