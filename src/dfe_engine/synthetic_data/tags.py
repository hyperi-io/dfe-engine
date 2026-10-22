#  Project:      dfe-engine
#  File:         synthetic_data/tags.py
#  Purpose:      Place the synthetic marker in an event's tags without changing their shape
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Where the synthetic marker lands in an event's ``tags``.

A pack whose own ``tags`` is an ECS keyword array (``Array(String)``) gets the
marker as a keyword, because the loader refuses an object for that column.
Every other pack gets the key ``tags.synthetic``.
"""

from typing import Any

from dfe_engine.synthetic_data.models import SyntheticDataError

SYNTHETIC_TAG = "synthetic"


def merge_tags(event: dict[str, Any], tags: dict[str, Any]) -> None:
    """Merge ``tags`` into ``event["tags"]``, keeping the shape the pack gave it.

    An object takes each key it does not already hold. A string or list becomes
    a list with ``synthetic`` appended once. An empty ``tags`` leaves a string
    or list untouched.

    Args:
        event: The generated event, edited in place.
        tags: The marker plus any extra tags the caller asked for.

    Raises:
        SyntheticDataError: If the event's ``tags`` is neither an object, a
            string nor a list, or if extra tags (anything but the marker) are
            asked of a string or list, which can only hold keywords.
    """
    node = event.setdefault("tags", {})
    if isinstance(node, dict):
        for key, value in tags.items():
            node.setdefault(key, value)
        return
    if not tags:
        return
    if isinstance(node, str):
        keywords = [node]
    elif isinstance(node, list):
        keywords = list(node)
    else:
        raise SyntheticDataError(
            f"cannot place the {SYNTHETIC_TAG!r} marker: the event's tags is "
            f"{type(node).__name__}, not an object, a string or a list"
        )
    extras = sorted(key for key in tags if key != SYNTHETIC_TAG)
    if extras:
        raise SyntheticDataError(
            f"extra tags {extras} need an object-shaped tags field, but this "
            "event's tags is a keyword list"
        )
    if tags.get(SYNTHETIC_TAG) and SYNTHETIC_TAG not in keywords:
        keywords.append(SYNTHETIC_TAG)
    event["tags"] = keywords
