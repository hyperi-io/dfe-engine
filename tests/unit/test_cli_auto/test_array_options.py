#  Project:      dfe-engine
#  File:         tests/unit/test_cli_auto/test_array_options.py
#  Purpose:      Array and object CLI options reach the API as the JSON the spec declares
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""An array-of-objects option reaches the API as the JSON list the spec declares.

Drives ``dfe sources from-catalogue`` through the real CLI against the in-process
engine, so a 422 from the API fails the test.
"""

import json

ENTRY_A = {"entry": "cisco-asa"}
ENTRY_B = {"entry": "okta", "name": "okta-prod", "archive": False}


def test_from_catalogue_creates_every_entry_given(harness):
    harness.seed_admin()

    result = harness.invoke(
        ["sources", "from-catalogue", "--entries", json.dumps([ENTRY_A, ENTRY_B])]
    )

    assert result.exit_code == 0, result.output
    reply = json.loads(result.stdout)
    assert reply["failed"] == []
    assert [row["source"] for row in reply["created"]] == ["cisco-asa", "okta-prod"]
    listed = harness.invoke(["--output", "json", "sources", "list"])
    stored = {row["name"] for row in json.loads(listed.stdout)}
    assert {"cisco-asa", "okta-prod"} <= stored


def test_from_catalogue_rejects_a_malformed_entry_before_sending(harness):
    harness.seed_admin()

    result = harness.invoke(["sources", "from-catalogue", "--entries", '[{"entry": '])

    assert result.exit_code == 2
    assert "entries" in result.output
