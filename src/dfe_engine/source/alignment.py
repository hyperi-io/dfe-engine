#  Project:      dfe-engine
#  File:         source/alignment.py
#  Purpose:      One DFE source is one connector type, across many connections
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A DFE source has one schema, so it has one connector type.

A source may poll MANY connections of that type - accounts, tenants, regions,
each with its own credential - and they all land in the one table, because they
all carry the one shape. A second type in the same source would carry a second
shape into that table, so the suite refuses it: the fetcher composed for a
source runs one type across all of its connections.

dfe-fetcher itself takes any mix on purpose. Standalone, without the suite and
without an engine deciding what a source is, a mixed config is a legitimate
thing to run, so nothing there refuses one. The alignment is the ENGINE's, and
it is applied where a source is written and again where its config is composed.

``settings.source.allow_mixed_fetcher_types`` turns it off for a deployment that
knowingly wants a mixed fetcher. Past that dial the suite's guarantees are gone:
no schema alignment, and the fetcher's own many-to-many semantics apply.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from scalo.logger import logger

MIXED_TYPES_SETTING = "source.allow_mixed_fetcher_types"
"""The dial, as an operator sets it in the engine's config file."""

MIXED_TYPES_ENV = "DFE_SOURCE_ALLOW_MIXED_FETCHER_TYPES"
"""The same dial as an environment variable, which is how a chart sets it."""

CONNECTION_TYPE_KEYS = ("type", "source_type")
"""Where a connection would name a family of its own.

The fetcher's connection structs carry no type at all - a connection inherits
its stanza's - so a family name under one of these keys is an operator asking a
single stanza to poll two different upstreams.
"""


class MixedConnectorTypesError(ValueError):
    """Raised when one fetcher config would carry more than one connector type."""


def _connections(stanza: Any) -> list[Mapping[str, Any]]:
    """The connection entries of one type's stanza, ignoring any other shape."""
    if not isinstance(stanza, Mapping):
        return []
    found = stanza.get("connections")
    if not isinstance(found, list):
        return []
    return [c for c in found if isinstance(c, Mapping)]


def connector_types(sources: Mapping[str, Any]) -> list[str]:
    """Every connector type a fetcher's ``sources`` block carries, sorted.

    A block's key IS its type, and a connection may name one of its own. Only a
    family the app manifest declares counts as a type, so an unrelated ``type``
    key inside a stanza is not mistaken for one.
    """
    # Imported at call time: the catalogue reaches back into this package.
    from dfe_engine.appmgmt.catalogue import source_types

    families = source_types()
    found = {str(family) for family in sources}
    for stanza in sources.values():
        for connection in _connections(stanza):
            for key in CONNECTION_TYPE_KEYS:
                named = connection.get(key)
                if isinstance(named, str) and named in families:
                    found.add(named)
    return sorted(found)


def allows_mixed(settings: Any) -> bool:
    """Whether this deployment has turned the alignment off."""
    return bool(settings.source.allow_mixed_fetcher_types)


def require_one_type(source: str, sources: Mapping[str, Any], settings: Any) -> None:
    """Refuse a fetcher config carrying more than one connector type.

    Args:
        source: The DFE source the config is composed for, named in the refusal.
        sources: The fetcher's ``sources`` block, type name to stanza.
        settings: Engine settings, read for the override dial.

    Raises:
        MixedConnectorTypesError: More than one type, and the dial is off.
    """
    types = connector_types(sources)
    if len(types) < 2:
        return
    if allows_mixed(settings):
        logger.warning(
            "fetcher config carries more than one connector type; it is unsupported "
            "and its records do not share a schema",
            source=source,
            types=types,
            setting=MIXED_TYPES_SETTING,
        )
        return
    raise MixedConnectorTypesError(
        f"source {source!r} carries {len(types)} connector types ({', '.join(types)}); "
        "a DFE source is one type across many connections, so each type needs its own "
        f"source. Set {MIXED_TYPES_ENV}=true to run a mixed fetcher unsupported."
    )
