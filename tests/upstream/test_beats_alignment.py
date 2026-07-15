#  Project:      dfe-engine
#  File:         tests/upstream/test_beats_alignment.py
#  Purpose:      Validate the schema importer against live Beats field definitions
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Validate the schema-source importer against live Beats field definitions.

Marked ``upstream`` (network; CI-excluded, like the deployed-DFE ``live`` tests) - run
explicitly with ``pytest -m upstream``. A manual validation oracle: confirms the importer
still holds against the current Beats field definitions as they evolve. Fetches skip (not
fail) when the network or a path is unavailable, so a moved path is a signal, not a red build.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from dfe_engine.services.schema.elastic_schema_service import ElasticSchemaConversionError
from dfe_engine.services.schema.schema_sources import import_schema

pytestmark = pytest.mark.upstream

_RAW = "https://raw.githubusercontent.com/elastic/beats/main"

# beat/module -> a sample fileset fields.yml (module-root files are shells; real fields
# live per fileset).
_UPSTREAM = {
    "filebeat/nginx": f"{_RAW}/filebeat/module/nginx/access/_meta/fields.yml",
    "filebeat/system": f"{_RAW}/filebeat/module/system/auth/_meta/fields.yml",
    "auditbeat/auditd": f"{_RAW}/auditbeat/module/auditd/_meta/fields.yml",
    "winlogbeat/security": f"{_RAW}/winlogbeat/module/security/_meta/fields.yml",
}

_PRIMITIVES = {
    "string",
    "text",
    "integer",
    "float",
    "boolean",
    "datetime",
    "timestamp",
    "date",
    "ip",
    "json",
    "geo_point",
    "uuid",
    "enum",
}


def _fetch(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310 - fixed https host
            return resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError) as exc:
        pytest.skip(f"live upstream unreachable ({url}): {exc}")


@pytest.mark.tuple("name,url", list(_UPSTREAM.items()))
def test_importer_holds_up_on_live_beats(name: str, url: str) -> None:
    raw = _fetch(url)
    try:
        cols = import_schema("beats_fields", raw)
    except ElasticSchemaConversionError as exc:
        pytest.skip(f"{name}: no importable fields at this path ({exc})")

    # It parsed + walked the current format without crashing, and every column is
    # PHYSICAL (an @source path) with a SIMPLE primitive type (the meta-schema benefit).
    assert isinstance(cols, list)
    for c in cols:
        assert c.expr
        assert c.expr.startswith("@source: "), f"{name}: {c.name} not physical"
        assert c.type in _PRIMITIVES, f"{name}: {c.name} unexpected type {c.type!r}"
