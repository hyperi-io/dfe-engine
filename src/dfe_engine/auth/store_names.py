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
store. The document stores key on the same names, so both backends share one rule,
:data:`dfe_engine.names.VALID_NAME`.
"""

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
