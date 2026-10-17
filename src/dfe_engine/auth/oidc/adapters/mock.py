#  Project:      dfe-engine
#  File:         auth/oidc/adapters/mock.py
#  Purpose:      Surface-B in-process mock directory for offline api-mode tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Surface-B: an in-process mock directory adapter.

The real api-mode adapters (Graph / Google Admin SDK / Okta) need a live tenant
and network access, which makes api-mode group resolution - and Entra's >200
group "overage" path in particular - impossible to exercise deterministically in
CI. This adapter stands in for a provider's directory API from a JSON fixture,
so the WHOLE api-mode surface (bulk sync, per-user enrichment, the overage
enrichment call) runs offline and reproducibly.

It is selected by ``GroupResolutionConfig.directory_backend == "mock"``. The
fixture path comes from the env var named in ``mock_directory_env`` (default
``DFE_OIDC_MOCK_DIRECTORY``). A sign-in's lookup never raises: a missing or
malformed fixture is no answer, as a live directory that could not be reached is.
The listing does not, as the live adapters' does not, so group sync never
records a fixture it could not read as a directory with no groups.

Fixture shape (JSON)::

    {
      "groups": [
        {"id": "<provider id>", "name": "dfe-admins", "email": "...", "description": "..."},
        ...
      ],
      "members": {
        "<directory id>": ["<group id>", "<group id>"],
        ...
      }
    }

``members`` is keyed by the provider's stable user object id (the value the RP
passes to ``resolve_user_groups`` - Entra's ``oid``, not ``sub``) and lists the
group *ids* that user belongs to, mirroring exactly what the live Graph
``transitiveMemberOf`` call would return.
"""

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from scalo.logger import logger

from dfe_engine.auth.oidc.adapters.base import (
    DirectoryCredentialError,
    DirectoryNotConfiguredError,
    OIDCGroupAdapter,
)
from dfe_engine.auth.oidc.models import GroupInfo

if TYPE_CHECKING:
    from dfe_engine.auth.oidc.models import OIDCProvider
    from dfe_engine.secrets import DfeSecrets


def _fixture_parts(data: object) -> tuple[list[dict], dict]:
    """The raw ``groups`` and ``members`` a parsed fixture holds.

    Raises:
        ValueError: The fixture is not the object the module docstring describes.
    """
    if not isinstance(data, dict):
        raise ValueError(f"the top level is a {type(data).__name__}, not an object")
    groups = data.get("groups", [])
    if not isinstance(groups, list) or not all(isinstance(item, dict) for item in groups):
        raise ValueError("'groups' is not a list of objects")
    members = data.get("members", {})
    if not isinstance(members, dict) or not all(
        value is None or isinstance(value, list) for value in members.values()
    ):
        raise ValueError("'members' is not an object of lists")
    return groups, members


class MockDirectoryAdapter(OIDCGroupAdapter):
    """Deterministic offline directory backed by a JSON fixture.

    Implements the full adapter contract from a static file: ``list_all_groups``
    (bulk sync), ``resolve_user_groups`` (the overage / per-user enrichment
    call), and a ``test_connection`` that reports the loaded fixture size.
    """

    def __init__(self, provider: OIDCProvider, *, secrets: DfeSecrets | None = None) -> None:
        super().__init__(provider, secrets=secrets)
        self._groups: list[GroupInfo] = []
        self._by_id: dict[str, GroupInfo] = {}
        self._members: dict[str, list[str]] = {}
        self._load_error: str = ""
        self._fixture_named = False
        self._load()

    def _load(self) -> None:
        """Read the fixture named by the provider's ``mock_directory_env``.

        Any problem leaves an empty directory and records the reason for
        ``test_connection`` and ``list_all_groups`` to surface, but never raises.
        """
        env_name = self._provider.groups.mock_directory_env or "DFE_OIDC_MOCK_DIRECTORY"
        path_str = os.environ.get(env_name)
        if not path_str:
            self._load_error = f"env var {env_name} not set"
            return
        self._fixture_named = True
        path = Path(path_str)
        if not path.is_file():
            self._load_error = f"fixture not found at {path}"
            logger.warning("mock directory: fixture missing", path=str(path), env=env_name)
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._load_error = f"fixture unreadable: {exc}"
            logger.warning("mock directory: fixture unreadable", path=str(path), error=str(exc))
            return

        try:
            raw_groups, raw_members = _fixture_parts(data)
        except ValueError as exc:
            self._load_error = f"fixture malformed: {exc}"
            logger.warning("mock directory: fixture malformed", path=str(path), error=str(exc))
            return

        for item in raw_groups:
            info = GroupInfo(
                id=str(item.get("id", "")),
                name=str(item.get("name", "")),
                email=str(item.get("email", "") or ""),
                description=str(item.get("description", "") or ""),
            )
            self._groups.append(info)
            if info.id:
                self._by_id[info.id] = info
        self._members = {str(k): [str(g) for g in (v or [])] for k, v in raw_members.items()}

    def _groups_for(self, directory_id: str) -> list[GroupInfo]:
        """The GroupInfo records for the group ids ``directory_id`` belongs to."""
        out: list[GroupInfo] = []
        for gid in self._members.get(directory_id, []):
            # A membership pointing at a group not in the directory still surfaces
            # as an id-only record, matching the live adapters' unfriendly fallback.
            out.append(self._by_id.get(gid) or GroupInfo(id=gid, name=gid))
        return out

    async def resolve_user_groups(self, directory_id: str) -> list[GroupInfo] | None:
        """The overage / enrichment call: this user's group memberships, or None when no fixture loaded."""
        if self._load_error:
            return None
        return self._groups_for(directory_id)

    async def list_all_groups(self) -> list[GroupInfo]:
        """Every group in the fixture (the bulk-sync source).

        Raises:
            DirectoryNotConfiguredError: No fixture is named.
            DirectoryCredentialError: The named fixture is missing or unreadable.
        """
        if self._load_error and not self._fixture_named:
            raise DirectoryNotConfiguredError
        if self._load_error:
            raise DirectoryCredentialError
        return list(self._groups)

    async def test_connection(self) -> tuple[bool, str]:
        """Report the loaded fixture, or the load error if it failed to read."""
        if self._load_error:
            return (False, f"mock directory not loaded: {self._load_error}")
        return (
            True,
            f"mock directory loaded -- {len(self._groups)} group(s), "
            f"{len(self._members)} member mapping(s)",
        )
