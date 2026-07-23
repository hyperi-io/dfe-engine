#  Project:      dfe-engine
#  File:         governance/seed.py
#  Purpose:      Seed the shipped Governed Ops action library into the deploy repo
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Put the standard action library into a deploy repo that has none.

WHY THIS EXISTS. Governed Ops reads actions and policies out of the deploy repo
(``governance/actions``, ``governance/policies``). The shipped library lives in
``dfe-deploy``, which is a TEMPLATE -- a real deployment instantiates its own
store, and until somebody hand-copies the library across, the API is wired,
authorised, documented, and returns an empty list. An operator opens the UI,
sees no dials, and reasonably concludes the feature is broken. This closes that
for EVERY deployment rather than one hand-seeded cluster.

CONTRACT -- non-destructive, per file:

- A file that already exists is left completely alone. That includes one an
  operator edited to change a detent, so seeding never fights a customisation.
- There is no marker file and no all-or-nothing gate, so a NEW action shipped in
  a later engine release lands beside the operator's edited ones.
- A DELETED file is put back, unless its name is listed in
  ``governance/.seed-ignore`` (one name per line, ``#`` comments). Without a
  tombstone, "deleted" and "never seeded" are indistinguishable, and guessing
  wrong either resurrects what the operator removed or silently ships nothing.

Seeded content is COMMITTED through the normal publish path, not written into
the working tree. A dirty working copy would be reverted by the next pull and
would never reach the API, which reads committed state.

The library is packaged with the engine (``governance/resources``) so it travels
with the image and needs no mounted volume.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from scalo.logger import logger

ACTIONS_SUBDIR = "governance/actions"
POLICIES_SUBDIR = "governance/policies"
IGNORE_FILE = "governance/.seed-ignore"
SEED_COMMIT_MESSAGE = "chore: seed the standard Governed Ops action library"

_RESOURCE_PACKAGE = "dfe_engine.governance.resources"


def _load_ignore(repo_root: Path) -> set[str]:
    """Names the operator has deliberately removed and does not want back."""
    ignore_path = repo_root / IGNORE_FILE
    if not ignore_path.is_file():
        return set()
    names: set[str] = set()
    for raw in ignore_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            names.add(line)
    return names


def _packaged(kind: str) -> dict[str, str]:
    """Map ``<name>.yaml`` to its content for one packaged resource kind."""
    package = f"{_RESOURCE_PACKAGE}.{kind}"
    try:
        root = resources.files(package)
    except (ModuleNotFoundError, FileNotFoundError):
        logger.warning(f"Governance seed package {package!r} missing; nothing to seed")
        return {}
    return {
        entry.name: entry.read_text(encoding="utf-8")
        for entry in sorted(root.iterdir(), key=lambda e: e.name)
        if entry.is_file() and entry.name.endswith(".yaml")
    }


def pending_seed(*, repo_root: Path) -> dict[str, str]:
    """Return {repo-relative path: content} for shipped files this repo lacks.

    Empty means nothing to do, which is the steady state on every start after the
    first. The caller commits the result; this function never writes.
    """
    ignore = _load_ignore(repo_root)
    artifacts: dict[str, str] = {}

    for kind, subdir in (("actions", ACTIONS_SUBDIR), ("policies", POLICIES_SUBDIR)):
        for name, content in _packaged(kind).items():
            if Path(name).stem in ignore:
                continue
            rel = f"{subdir}/{name}"
            if (repo_root / rel).exists():
                continue
            artifacts[rel] = content

    return artifacts
