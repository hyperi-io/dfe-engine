#  Project:      dfe-engine
#  File:         bootstrap.py
#  Purpose:      Ensure schemas/config directories exist and seed schemas once
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Ensure the schemas and config storage directories exist, seeding schemas once.

Schemas are seeded from the image-baked ``dfe-schemas`` tree when the schemas directory's ``.seeded`` marker is absent or names a different engine version, so an upgrade refreshes the shipped defaults over a tree an older image seeded while leaving files the deployment added; config is only ensured to exist. The config directory falls back to a baked default when unset; the schemas directory is bootstrapped only when configured (the container image sets ``DFE_SCHEMAS_DIR``), so an unconfigured schemas directory is skipped rather than forced onto an absolute default path. Both directories are expected to sit on a persistent volume.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from scalo.logger import logger

from dfe_engine import __version__
from dfe_engine.schema.schema_loader import DEFAULT_SCHEMAS_SEED_DIR, SEED_DIR_ENV_VAR
from dfe_engine.settings import DFESettings

DEFAULT_CONFIG_DIR = "/app/config"
SEED_MARKER_NAME = ".seeded"


def _seed_stamp() -> str:
    """Identify the shipped tree, so a marker written by another version re-seeds."""
    return f"dfe-engine {__version__}"


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
    """Ensure the config directory exists and, when configured, seed schemas on first run.

    The config directory is filled with its baked default when unset. The schemas directory is bootstrapped only when ``schemas.schemas_dir`` is set; when it is unset the schemas bootstrap is skipped entirely, so the downstream registry bootstrap (which is itself gated on a configured directory) simply observes no schemas rather than an invented absolute default path.
    """
    config_dir = _resolve_dir(configured=settings.config_dir, default=DEFAULT_CONFIG_DIR)
    _ensure_dir(path=config_dir)
    settings.config_dir = str(config_dir)

    if not (settings.schemas.schemas_dir):
        logger.info("Schemas directory not configured; skipping schema storage bootstrap")
        return

    schemas_dir = Path(settings.schemas.schemas_dir)
    seed_dir = Path(os.environ.get(SEED_DIR_ENV_VAR, DEFAULT_SCHEMAS_SEED_DIR))

    _ensure_dir(path=schemas_dir)
    settings.schemas.schemas_dir = str(schemas_dir)

    marker = schemas_dir / SEED_MARKER_NAME
    stamp = _seed_stamp()
    seeded_by = marker.read_text().strip() if marker.exists() else ""
    if seeded_by == stamp:
        logger.info(f"Schemas already seeded by {stamp!r}; skipping")
        return
    if not (seed_dir.is_dir()):
        logger.warning(f"Schema seed {str(seed_dir)!r} not found; leaving schemas dir empty")
        return

    _copy_seed(schemas_dir=schemas_dir, seed_dir=seed_dir)
    marker.write_text(f"{stamp}\n")
    if seeded_by:
        logger.info(f"Re-seeded schemas over a tree seeded by {seeded_by!r}; now {stamp!r}")
        return
    logger.info(f"Seeded schemas from {str(seed_dir)!r} into {str(schemas_dir)!r}")
