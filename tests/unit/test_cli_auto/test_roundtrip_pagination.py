#  Project:      dfe-engine
#  File:         tests/unit/test_cli_auto/test_roundtrip_pagination.py
#  Purpose:      Real round-trips against the in-process engine, incl. page walking
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Real round-trips against the in-process engine, incl. pagination."""

import json
from urllib.parse import parse_qsl, urlsplit

from scalo.cli.openapi import Response

from dfe_engine.cli.auto.app import _PageWalker


def _json(harness, *args):
    result = harness.invoke(["--output", "json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _create_org(harness, name: str) -> None:
    result = harness.invoke(["orgs", "create", "--name", name])
    assert result.exit_code == 0, result.output


def test_create_list_get_roundtrip(harness):
    harness.seed_admin()
    result = harness.invoke(["orgs", "create", "--name", "acme", "--display-name", "Acme Corp"])
    assert result.exit_code == 0, result.output
    created = json.loads(result.stdout)
    assert (created["name"], created["display_name"]) == ("acme", "Acme Corp")

    assert "acme" in [row["name"] for row in _json(harness, "orgs", "list")]
    assert _json(harness, "orgs", "get", "acme")["name"] == "acme"


def test_delete_needs_yes_off_a_terminal(harness):
    harness.seed_admin()
    _create_org(harness, "todelete")
    blocked = harness.invoke(["orgs", "delete", "todelete"])
    assert blocked.exit_code == 2
    assert "pass --yes" in blocked.output

    ok = harness.invoke(["--yes", "orgs", "delete", "todelete"])
    assert ok.exit_code == 0, ok.output
    assert "todelete" not in [row["name"] for row in _json(harness, "orgs", "list")]


def test_list_walks_every_page(harness):
    harness.seed_admin()
    for i in range(5):
        _create_org(harness, f"p{i}")
    # Page size 2 over six orgs (the reserved default org included) takes three requests.
    assert len(_json(harness, "orgs", "list", "--per-page", "2")) == 6


def test_explicit_page_gets_that_page_alone(harness):
    harness.seed_admin()
    for i in range(5):
        _create_org(harness, f"n{i}")
    assert len(_json(harness, "orgs", "list", "--per-page", "2", "--page", "2")) == 2


def _walk(pages: dict[int, dict], url: str):
    """Return the merged items and requested URLs from ``_PageWalker`` using canned pages."""
    asked: list[str] = []

    class Canned:
        def send(self, method, url, *, headers, credentials, content, timeout):
            asked.append(url)
            page = int(dict(parse_qsl(urlsplit(url).query)).get("page", 1))
            return Response(200, {}, json.dumps(pages[page]).encode())

    reply = _PageWalker(Canned()).send(
        "GET", url, headers={}, credentials={}, content=None, timeout=1.0
    )
    return json.loads(reply.content), asked


def test_walker_keeps_repeated_filters_on_every_page():
    pages = {
        1: {"items": [1], "page": 1, "next_page": 2},
        2: {"items": [2], "page": 2, "next_page": None},
    }

    items, asked = _walk(pages, "http://t/api/v1/schemas?schema_type=a&schema_type=b")

    assert items == [1, 2]
    assert asked[1] == "http://t/api/v1/schemas?schema_type=a&schema_type=b&page=2"


def test_walker_stops_when_a_server_repeats_a_page():
    pages = {
        1: {"items": [1], "page": 1, "next_page": 2},
        2: {"items": [1], "page": 1, "next_page": 2},
    }

    items, asked = _walk(pages, "http://t/api/v1/sources")

    assert items == [1, 1]
    assert len(asked) == 2
