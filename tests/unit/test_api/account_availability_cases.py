#  Project:      dfe-engine
#  File:         tests/unit/test_api/account_availability_cases.py
#  Purpose:      Case tables for whether a new account could take a name and hold an email
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for whether a new account could take a name and hold an email."""

from typing import TypedDict

EMAIL_TAKEN = "Another account already uses this email"


class AvailabilityCase(TypedDict):
    """A name and email asked about while kaz (vouched email), jo (self-set email) and kaz's link record okta-kaz are stored."""

    id: str
    email: str
    expected_email: str
    expected_username: str
    username: str


AVAILABILITY_CASES: list[AvailabilityCase] = [
    {
        "id": "a_free_name_and_email_are_available",
        "email": "newbie@example.test",
        "expected_email": "",
        "expected_username": "",
        "username": "newbie",
    },
    {
        "id": "a_stored_name_is_taken",
        "email": "",
        "expected_email": "",
        "expected_username": "Account 'kaz' already exists",
        "username": "kaz",
    },
    {
        "id": "a_name_differing_only_in_case_is_taken",
        "email": "",
        "expected_email": "",
        "expected_username": "Account 'kaz' already exists",
        "username": "KAZ",
    },
    {
        "id": "a_link_records_name_is_taken",
        "email": "",
        "expected_email": "",
        "expected_username": "Account 'okta-kaz' already exists",
        "username": "okta-kaz",
    },
    {
        "id": "an_invalid_name_is_refused",
        "email": "",
        "expected_email": "",
        "expected_username": "Invalid account name: '-bad'",
        "username": "-bad",
    },
    {
        "id": "a_vouched_email_is_taken",
        "email": "KAZ@example.test",
        "expected_email": EMAIL_TAKEN,
        "expected_username": "",
        "username": "newbie",
    },
    {
        "id": "a_self_set_email_is_available",
        "email": "jo@example.test",
        "expected_email": "",
        "expected_username": "",
        "username": "newbie",
    },
]
