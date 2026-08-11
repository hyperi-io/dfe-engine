#  Project:      dfe-engine
#  File:         env_files.py
#  Purpose:      Load optional .env files before settings resolution
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Load developer ``.env`` files into ``os.environ`` for ``dfe-engine run``.

Shell-exported variables always win (``override=False``). Search order:

1. ``DFE_ENV_FILE`` when set
2. ``./.env`` in the current working directory
3. ``$DFE_CONFIG_DIR/.env`` when ``DFE_CONFIG_DIR`` is set
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env_files() -> None:
    """Populate missing env vars from the first existing ``.env`` candidates."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    candidates: list[Path] = []
    explicit = os.environ.get("DFE_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path.cwd() / ".env")
    config_dir = os.environ.get("DFE_CONFIG_DIR", "").strip()
    if config_dir:
        candidates.append(Path(config_dir) / ".env")

    seen: set[Path] = set()
    for path in candidates:
        resolved = path.expanduser().resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        load_dotenv(resolved, override=False)
