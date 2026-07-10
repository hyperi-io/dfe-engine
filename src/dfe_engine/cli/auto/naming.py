#  Project:      dfe-engine
#  File:         cli/auto/naming.py
#  Purpose:      Derive CLI group path + verb from an OpenAPI path + method
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Map a REST ``(path, method)`` onto a ``(group_path, verb)`` command name.

Battle-tested aws-cli/gcloud conventions, kept gcloud-lean:

- ``GET`` collection             -> ``list``
- ``GET`` item (trailing param)  -> ``describe``
- ``POST`` collection            -> ``create``
- ``PUT``/``PATCH`` item         -> ``update``
- ``DELETE`` item                -> ``delete``
- ``POST``/other on ``/{id}/<action>`` or a collection ``/<action>`` -> the
  ``<action>`` segment, kebab-cased, as the verb.

Group nesting = the resource path segments (kebab). ``/api/v1/auth/accounts`` ->
group ``['auth','accounts']``; ``/api/v1/orgs`` -> ``['orgs']``. Path params are
never part of the group - they become command arguments.

A collection (POST -> create) is told apart from a leaf action (POST -> <action>)
by whether the collection has a sibling item route (``.../{param}``); that set is
computed once in ``spec.iter_operations`` and passed in.
"""

from __future__ import annotations

from .vendor.xform import xform_name

_API_PREFIX = ("api", "v1")


def is_param(segment: str) -> bool:
    """True for an OpenAPI path template segment like ``{name}``."""
    return segment.startswith("{") and segment.endswith("}")


def api_segments(path: str) -> list[str]:
    """Path segments with the leading ``/api/v1`` stripped."""
    parts = [p for p in path.split("/") if p]
    if parts[: len(_API_PREFIX)] == list(_API_PREFIX):
        parts = parts[len(_API_PREFIX) :]
    return parts


def _kebab(name: str) -> str:
    # xform handles CamelCase/acronym runs; also fold snake_case to kebab.
    return xform_name(name.replace("_", "-"), "-").replace("_", "-")


def derive_group_and_verb(
    path: str, method: str, collections_with_item: set[str]
) -> tuple[list[str], str]:
    """Return ``(group_path, verb)`` for one operation."""
    method = method.lower()
    segments = api_segments(path)
    if not segments:
        return [], method

    literals = [s for s in segments if not is_param(s)]
    trailing_param = is_param(segments[-1])

    if trailing_param:
        # Item operation: /orgs/{name}, /auth/accounts/{username}
        group = [_kebab(s) for s in literals]
        if method == "get":
            return group, "describe"
        if method in ("put", "patch"):
            return group, "update"
        if method == "delete":
            return group, "delete"
        # POST/other on an item without a trailing action segment - rare; name it
        # after the method so it is at least addressable.
        return group, _kebab(method)

    # Trailing literal. Is this literal a REST collection, or an action verb?
    collection_key = "/".join(segments)
    is_collection = collection_key in collections_with_item

    if method == "get":
        # GET on a collection (or a sub-list) -> list; group includes the leaf.
        return [_kebab(s) for s in literals], "list"

    if is_collection and method == "post":
        return [_kebab(s) for s in literals], "create"

    if is_collection and method in ("put", "patch"):
        return [_kebab(s) for s in literals], "update"

    if is_collection and method == "delete":
        return [_kebab(s) for s in literals], "delete"

    # Leaf action: /{id}/<action> or collection /<action>. The trailing literal
    # becomes the verb; the group is the resource path before it (params dropped).
    verb = _kebab(segments[-1])
    group = [_kebab(s) for s in segments[:-1] if not is_param(s)]
    return group, verb
