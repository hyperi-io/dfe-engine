#  Project:      dfe-engine
#  File:         auth/setup_marker.py
#  Purpose:      Record that first-run setup finished, so the wizard never reopens
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The marker that first-run setup finished.

Setup status is otherwise worked out from live state, so deleting the last customer org or real user would send every signed-in user back to the wizard. The marker sits in the config directory with the orgs and accounts it speaks for, so wiping that directory reopens the wizard along with them.
"""

from pathlib import Path

from scalo.logger import logger

_MARKER = "setup-complete"


def clear(*, config_dir: str) -> None:
    """Remove the marker, which only the e2e reset has reason to do."""
    if config_dir:
        (Path(config_dir) / _MARKER).unlink(missing_ok=True)


def is_recorded(*, config_dir: str) -> bool:
    """Whether setup finished here, always false with no config directory to hold the marker."""
    return bool(config_dir) and (Path(config_dir) / _MARKER).exists()


def record(*, config_dir: str) -> None:
    """Write the marker. A failed write is logged and the next status request tries again."""
    if not (config_dir):
        return
    try:
        (Path(config_dir) / _MARKER).touch()
    except OSError as exc:
        logger.warning("Could not record that setup finished", error=str(exc))
