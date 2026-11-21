#  Project:      dfe-engine
#  File:         tests/unit/test_api/deps_cases.py
#  Purpose:      Case tables for the api.deps unit tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the api.deps unit tests."""

from typing import TypedDict

from dfe_engine.auth.accounts import Account
from tests.unit.test_auth.factories import make_account, make_linked_identity

_OKTA_SUBJECT = "00u15mxs3ecygt7oj698"


def _link_record(*, key: str, linked_to: str, subject: str) -> Account:
    """A link record as a linking login leaves it."""
    return make_account(
        external=True, linked_to=linked_to, source_provider="okta", subject=subject, username=key
    )


def _person(*, key: str, subject: str, username: str = "kaz") -> Account:
    """A local account listing one okta sign-in."""
    identity = make_linked_identity(key=key, provider="okta", subject=subject)
    return make_account(identities=[identity], username=username)


class AccountForSessionSubjectCase(TypedDict):
    id: str
    expected_username: str | None
    stored: list[Account]
    subject: str


ACCOUNT_FOR_SESSION_SUBJECT_CASES: list[AccountForSessionSubjectCase] = [
    {
        "id": "a_link_record_binds_its_person",
        "expected_username": "kaz",
        "stored": [
            _person(key=_OKTA_SUBJECT, subject=_OKTA_SUBJECT),
            _link_record(key=_OKTA_SUBJECT, linked_to="kaz", subject=_OKTA_SUBJECT),
        ],
        "subject": _OKTA_SUBJECT,
    },
    {
        "id": "a_link_record_at_the_stem_binds_its_person",
        "expected_username": "kaz",
        "stored": [
            _person(key="jane-corp-com", subject="Jane@Corp.com"),
            _link_record(key="jane-corp-com", linked_to="kaz", subject="Jane@Corp.com"),
        ],
        "subject": "Jane@Corp.com",
    },
    {
        "id": "a_missing_person_binds_nothing",
        "expected_username": None,
        "stored": [_link_record(key=_OKTA_SUBJECT, linked_to="kaz", subject=_OKTA_SUBJECT)],
        "subject": _OKTA_SUBJECT,
    },
    {
        "id": "a_chained_link_binds_nothing",
        "expected_username": None,
        "stored": [
            _link_record(key="kaz", linked_to="root", subject="kaz"),
            _link_record(key=_OKTA_SUBJECT, linked_to="kaz", subject=_OKTA_SUBJECT),
            _person(key=_OKTA_SUBJECT, subject=_OKTA_SUBJECT, username="root"),
        ],
        "subject": _OKTA_SUBJECT,
    },
    {
        "id": "a_person_no_longer_listing_the_sign_in_binds_nothing",
        "expected_username": None,
        "stored": [
            make_account(username="kaz"),
            _link_record(key=_OKTA_SUBJECT, linked_to="kaz", subject=_OKTA_SUBJECT),
        ],
        "subject": _OKTA_SUBJECT,
    },
    {
        "id": "a_person_listing_another_subject_at_the_key_binds_nothing",
        "expected_username": None,
        "stored": [
            _person(key="jane-corp-com", subject="jane@corp.com"),
            _link_record(key="jane-corp-com", linked_to="kaz", subject="Jane@Corp.com"),
        ],
        "subject": "Jane@Corp.com",
    },
    {
        "id": "a_person_binds_its_own_name",
        "expected_username": "kaz",
        "stored": [_person(key=_OKTA_SUBJECT, subject=_OKTA_SUBJECT)],
        "subject": "kaz",
    },
]
