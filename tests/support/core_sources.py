#  Project:      dfe-engine
#  File:         tests/support/core_sources.py
#  Purpose:      The landing source definition dfe-schemas ships, for tests that need it seeded
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The seeded landing definition, shared by every test whose app startup seeds it.

In a deployment this file arrives from dfe-schemas through the image-baked schema
seed. Tests cannot rely on the installed wheel carrying it - the engine pins a
dfe-schemas that predates ``sources/`` - so they write this copy into their own
tmp schemas directory instead.

``TestShippedDefinition`` in ``tests/unit/test_source/test_core_sources.py`` is the
drift guard: it compares this dict against the real file once the installed
dfe-schemas ships one.
"""

from __future__ import annotations

from pathlib import Path

from dfe_engine.source.core_sources import LANDING_SOURCE_FILE, SOURCES_SUBDIR
from dfe_engine.yaml_utils import yaml_dump

# What dfe-schemas ships at sources/main.yaml. No `source` name: the engine fills
# it from the landing-table setting.
LANDING_DEFINITION = {
    "resource_type": "core",
    "description": "Where the loader puts a record it has nowhere else to send",
    "state": "active",
    "deployed_version": "1.0.0",
    "current": "1.0.0",
    "versions": {
        "1.0.0": {
            "date_time": "2026-09-11",
            "header": {"type": "timeseries", "version": "1.0.0"},
            "schema": {},
        }
    },
}


def write_landing_definition(*, schemas_dir: Path) -> Path:
    """Put the definition where the schema seed would, and return its path."""
    sources_dir = Path(schemas_dir) / SOURCES_SUBDIR
    sources_dir.mkdir(parents=True, exist_ok=True)
    path = sources_dir / LANDING_SOURCE_FILE
    yaml_dump(LANDING_DEFINITION, path)
    return path
