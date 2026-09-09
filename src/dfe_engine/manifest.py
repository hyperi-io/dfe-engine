#  Project:      dfe-engine
#  File:         manifest.py
#  Purpose:      Read a declared-data file the engine reflects rather than owns
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One way to find and read a manifest, for every manifest the engine reflects.

Several files here are DATA another repo owns and this one only reads: the app
manifest from dfe-infra, the source catalogue a transform ships. They resolve the
same way - an explicit path, then an environment variable a deployment sets to a
mounted copy, then whatever the engine falls back on - and they fail the same
way, so the resolution and the reading live here once.

Nothing about any particular manifest is here. What a document MEANS is its own
module's business; this only hands that module a mapping, or says why it cannot.
"""

from __future__ import annotations

import os
from pathlib import Path

from dfe_engine.yaml_utils import yaml_load


class ManifestError(ValueError):
    """Raised when a manifest cannot be read or is not the shape it claims."""


def manifest_path(explicit: Path | str | None, env_var: str, fallback: Path | None) -> Path | None:
    """Where a manifest is read from: the argument, the environment, the fallback.

    Returns None when nothing names a file - a deployment that mounts no
    catalogue has none, which is different from one whose catalogue is broken.
    """
    if explicit:
        return Path(explicit)
    from_env = os.getenv(env_var)
    if from_env:
        return Path(from_env)
    return fallback


def read_manifest(path: Path, *, what: str) -> dict:
    """The document at *path* as a mapping, or why it is not one.

    ``what`` names the manifest in every message, because the operator reading it
    knows the file by that name and not by this module.
    """
    if not path.is_file():
        raise ManifestError(f"{what} not found: {path}")
    try:
        doc = yaml_load(path) or {}
    except Exception as exc:
        raise ManifestError(f"{what} {path} is not readable: {exc}") from exc
    if not isinstance(doc, dict):
        raise ManifestError(f"{what} {path} is not a mapping")
    return doc
