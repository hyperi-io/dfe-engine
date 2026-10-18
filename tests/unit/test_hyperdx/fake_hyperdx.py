#  Project:      dfe-engine
#  File:         tests/unit/test_hyperdx/fake_hyperdx.py
#  Purpose:      Stand-ins for the fork's /dfe/sources surface, for tests of HyperDX source writes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Stand-ins for the fork's ``/dfe/sources`` surface: one in memory, one that raises."""

from typing import Any

# Two human teams plus the engine's own service team, which has no connection.
HUMAN_TEAMS = ["dfe-admins", "customer-acme"]
SERVICE_TEAM = "dfe"


class Exploding:
    """A client that raises rather than returning the non-fatal None."""

    async def delete_dfe_source(self, name: str) -> dict[str, Any] | None:
        """Raise as a client with no failure contract would."""
        raise RuntimeError("connection refused")

    async def list_dfe_sources(self) -> dict[str, Any] | None:
        """Raise as a client with no failure contract would."""
        raise RuntimeError("connection refused")

    async def put_dfe_source(self, name: str, spec: dict[str, Any]) -> dict[str, Any] | None:
        """Raise as a client with no failure contract would."""
        raise RuntimeError("connection refused")


class FakeHyperDX:
    """The fork's ``/dfe/sources`` surface in memory, with the client's failure contract.

    The real client is non-fatal: every call returns None rather than raising, so
    ``reachable=False`` is what an unreachable HyperDX looks like to a caller.
    """

    def __init__(self, *, reachable: bool = True, owned: bool = True) -> None:
        self.reachable = reachable
        # False stands for the fork's 409: the name belongs to its seeded set.
        self.owned = owned
        self.sources: dict[str, dict[str, Any]] = {}
        self.puts = 0
        self.deletes = 0

    async def put_dfe_source(self, name: str, spec: dict[str, Any]) -> dict[str, Any] | None:
        if not self.reachable or not self.owned:
            return None
        self.puts += 1
        self.sources[name] = spec
        return {"name": name, "written": list(HUMAN_TEAMS), "skipped": [SERVICE_TEAM]}

    async def delete_dfe_source(self, name: str) -> dict[str, Any] | None:
        if not self.reachable:
            return None
        self.deletes += 1
        removed = HUMAN_TEAMS if self.sources.pop(name, None) is not None else []
        return {"name": name, "removed": list(removed)}

    async def list_dfe_sources(self) -> dict[str, Any] | None:
        if not self.reachable:
            return None
        return {
            "teams": [
                {
                    "team": f"id-{team}",
                    "teamName": team,
                    "sources": [
                        {"id": f"{team}-{name}", "name": name, "from": spec["from"]}
                        for name, spec in self.sources.items()
                    ],
                }
                for team in HUMAN_TEAMS
            ]
        }
