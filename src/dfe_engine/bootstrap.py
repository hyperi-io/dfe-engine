#  Project:      dfe-engine
#  File:         bootstrap.py
#  Purpose:      Ensure schemas/config directories exist and seed schemas once
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Ensure the schemas and config storage directories exist, seeding schemas once.

Schemas are seeded from the image-baked ``dfe-schemas`` submodule only when the schemas directory has no ``.seeded`` marker, so redeployments never re-seed; config is only ensured to exist. Both directories fall back to a baked default when their environment variable is unset and are expected to sit on a persistent volume.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from hyperi_pylib.logger import logger

from dfe_engine.settings import DFESettings

DEFAULT_CONFIG_DIR = "/app/config"
DEFAULT_SCHEMAS_DIR = "/app/schemas"
DEFAULT_SCHEMAS_SEED_DIR = "/app/schemas-seed"
SEED_DIR_ENV_VAR = "DFE_SCHEMAS_SEED_DIR"
SEED_MARKER_NAME = ".seeded"


def _copy_seed(*, schemas_dir: Path, seed_dir: Path) -> None:
    """Copy every entry from the seed directory into the schemas directory."""
    for entry in sorted(seed_dir.iterdir()):
        if entry.name == ".git":
            continue
        destination = schemas_dir / entry.name
        if entry.is_dir():
            shutil.copytree(dirs_exist_ok=True, dst=destination, src=entry)
        else:
            shutil.copy2(dst=destination, src=entry)


def _ensure_dir(*, path: Path) -> None:
    """Create the directory and any missing parents; a no-op if it already exists."""
    path.mkdir(exist_ok=True, parents=True)


def _resolve_dir(*, configured: str, default: str) -> Path:
    """Return the configured directory, or the baked default when it is unset."""
    resolved = (configured) or (default)
    return Path(resolved)


def ensure_storage(*, settings: DFESettings) -> None:
    """Ensure config and schemas directories exist and seed schemas on first run.

    Any unset directory setting is filled with its baked default so the downstream registry bootstrap observes the effective paths.
    """
    config_dir = _resolve_dir(configured=settings.config_dir, default=DEFAULT_CONFIG_DIR)
    schemas_dir = _resolve_dir(configured=settings.schemas.schemas_dir, default=DEFAULT_SCHEMAS_DIR)
    seed_dir = Path(os.environ.get(SEED_DIR_ENV_VAR, DEFAULT_SCHEMAS_SEED_DIR))

    _ensure_dir(path=config_dir)
    _ensure_dir(path=schemas_dir)

    settings.config_dir = str(config_dir)
    settings.schemas.schemas_dir = str(schemas_dir)

    marker = schemas_dir / SEED_MARKER_NAME
    if marker.exists():
        logger.info("Schemas already seeded (marker %r present); skipping", str(marker))
        return
    if not (seed_dir.is_dir()):
        logger.warning("Schema seed %r not found; leaving schemas dir empty", str(seed_dir))
        return

    _copy_seed(schemas_dir=schemas_dir, seed_dir=seed_dir)
    marker.write_text("Seeded by dfe_engine.bootstrap.ensure_storage.\n")
    logger.info("Seeded schemas from %r into %r", str(seed_dir), str(schemas_dir))
