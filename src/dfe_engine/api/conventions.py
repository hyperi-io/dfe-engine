#  Project:      dfe-engine
#  File:         api/conventions.py
#  Purpose:      The resource hierarchy every engine operation is named by and checked against
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The resource hierarchy the engine API is named by and held to.

:data:`HIERARCHY` derives every operation id from its path
(``<root>.<child...>.<verb>``), and :func:`scalo.api.install` writes the
``x-scalo`` extension onto each operation from the same model. The UI client,
the ``dfe`` CLI and the MCP tools all read their names from there, so nothing
else names an operation.
"""

from collections.abc import Sequence

from fastapi.routing import APIRoute, iter_route_contexts
from scalo.api import Hierarchy
from starlette.routing import BaseRoute

# The root resources of the hierarchy the API moves to.
ROOTS = frozenset(
    {
        "deployment",
        "governance",
        "identity",
        "ingest",
        "orgs",
        "pipelines",
        "schemas",
        "sources",
        "system",
    }
)

# Resources with no id. A singleton may have children.
SINGLETONS = frozenset(
    {
        "attributes",
        "ch-rbac",
        "compat",
        "contract",
        "defaults",
        "deployment",
        "governance",
        "health",
        "identity",
        "ingest",
        "retention",
        "sensitive-attributes",
        "state",
        "system",
        "usage",
    }
)

# Namespaces the protocol routes sit under, which have no id either.
PROTOCOL_NAMESPACES = frozenset({"auth", "scim", "v2"})

# Namespaces today's routers use with no id, declared only where two operations would
# otherwise share an id. Each goes when its router moves into the hierarchy.
UNPLACED_NAMESPACES = frozenset({"gitops", "repository", "sigma"})

# Collection names that are plural without ending in "s".
PLURALS = frozenset({"library"})

HIERARCHY = Hierarchy(
    base_path="/api/v1",
    singletons=SINGLETONS | PROTOCOL_NAMESPACES | UNPLACED_NAMESPACES,
    roots=ROOTS,
    max_depth=3,
    plurals=PLURALS,
    tool_prefix="dfe_",
)

# Routers whose shape a protocol fixes: token issue, OIDC, SCIM (RFC 7644), JWKS, the e2e helpers.
PROTOCOL_ROUTERS = (
    "dfe_engine.api.e2e",
    "dfe_engine.api.v1.auth",
    "dfe_engine.api.v1.oidc_login",
    "dfe_engine.api.v1.scim",
    "dfe_engine.api.well_known",
)


def _is_protocol(module: str) -> bool:
    return any(module == name or module.startswith(f"{name}.") for name in PROTOCOL_ROUTERS)


def protocol_paths(routes: Sequence[BaseRoute]) -> list[str]:
    """Every path a protocol router puts in the spec, which the API conventions exempt.

    Args:
        routes: The application's routes, included routers unexpanded.

    Returns:
        The route paths, sorted.
    """
    found = set()
    for context in iter_route_contexts(routes):
        endpoint = context.endpoint
        if not isinstance(context.original_route, APIRoute) or endpoint is None:
            continue
        if not context.include_in_schema:
            continue
        if _is_protocol(endpoint.__module__) and context.path_format:
            found.add(context.path_format)
    return sorted(found)
