#  Project:      dfe-engine
#  File:         auth/store_names.py
#  Purpose:      The name rule every account, group and API key store keys on
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The key every account, group and API key store looks a name up under.

A YAML store joins the name onto its directory as ``{name}.yaml``, so a name
from a request that reached that join unchecked would resolve a path outside the
store. Every key follows :data:`dfe_engine.names.VALID_NAME`.
"""

import os
from pathlib import Path

from dfe_engine import names


def store_key(name: str) -> str:
    """Return *name* as the key a store looks it up under.

    Args:
        name: An account, group or API key name, often straight off a request.

    Returns:
        *name*, unchanged.

    Raises:
        KeyError: Nothing can be stored under *name*, so a lookup of it finds nothing.
    """
    if not names.VALID_NAME.match(name):
        raise KeyError(name)
    return name


def case_twin(directory: Path, name: str) -> str | None:
    """Return the name stored in *directory* that differs from *name* only in case, or None.

    Names are unique case-insensitively, so ``Alice`` and ``alice`` cannot both be stored.
    """
    folded = name.lower()
    try:
        entries = os.listdir(directory)
    except OSError:
        # Nothing is stored where nothing can be listed, and the write reports why.
        return None
    for entry in entries:
        stem, _, ext = entry.rpartition(".")
        if ext == "yaml" and stem != name and stem.lower() == folded:
            return stem
    return None
