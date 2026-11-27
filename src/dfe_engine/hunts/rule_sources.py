#  Project:      dfe-engine
#  File:         hunts/rule_sources.py
#  Purpose:      Tie a rule to the registered source it reads
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A rule is saved against a registered source. Rules stored before that held a table.

A stored rule with no ``source`` whose ``source_table`` is a registered source's label
is read as that source, since a source's table carries its label. The rest name no
registered source and are listed, never dropped, so an author can re-save each one.
"""

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from scalo.logger import logger

from dfe_engine.source.registry import SourceNotFoundError


def source_check(registry: Any) -> Callable[[str], bool]:
    """A predicate for whether a label names a source in the registry that loads."""

    def is_source(label: str) -> bool:
        try:
            registry.get_source(label)
        # A label no file can carry, or a stored doc that no longer validates, names no usable source.
        except SourceNotFoundError, ValueError:
            return False
        return True

    return is_source


def resolved_source(
    source: str | None, source_table: str | None, is_source: Callable[[str], bool]
) -> str | None:
    """The registered source a stored rule reads, or None when it names none.

    Args:
        source: The rule's stored ``source``.
        source_table: The rule's stored ``source_table``.
        is_source: Whether a label names a registered source.

    Returns:
        The stored source when it is registered, else the table when that is a registered
        label, else None.
    """
    for candidate in (source, source_table):
        if candidate and is_source(candidate):
            return candidate
    return None


def unsourced_rules(
    rows: Iterable[Mapping[str, Any]], is_source: Callable[[str], bool]
) -> list[str]:
    """The names of the stored rules that read no registered source, sorted.

    Args:
        rows: The registry's listing, each row carrying ``name``, ``source`` and ``source_table``.
        is_source: Whether a label names a registered source.
    """
    return sorted(
        str(row["name"])
        for row in rows
        if resolved_source(row.get("source"), row.get("source_table"), is_source) is None
    )


def log_unsourced_rules(
    rows: Iterable[Mapping[str, Any]], is_source: Callable[[str], bool]
) -> None:
    """Log one line naming every stored rule that reads no registered source."""
    names = unsourced_rules(rows, is_source)
    if names:
        logger.warning(
            "stored rules name no registered source, so save each with one",
            count=len(names),
            rules=names,
        )
