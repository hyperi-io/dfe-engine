#  Project:      dfe-engine
#  File:         api/e2e/seed/logs.py
#  Purpose:      Git-operation-log seeder primitives for e2e-server
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Git operation log seeding. Public methods are the scripts. Privates are reusable.

The log is the deploy repo's commit history, so each seeded operation is its
own commit. The script runs outside the seed batch, which would otherwise
fold every write into one entry.
"""

from __future__ import annotations

from dfe_engine.api.e2e.seed.base import Seed
from dfe_engine.api.e2e.seed.sources import SEED_ACTOR
from dfe_engine.gitcrud.commit_policy import CommitContext, build_message
from dfe_engine.gitcrud.engine import GitCrud
from dfe_engine.gitcrud.log import read_log

_CLASS = "infravars"
"""Run-state class ``reset_all`` already clears, apart from the deployer's baseline."""

_MARKER = "searchable-log"
_SUMMARY = "searchable operation"


def searchable_log_scopes() -> list[str]:
    """Commit scopes ``seed_searchable_git_operation_logs`` writes."""
    return [f"searchable-{num}" for num in range(50)]


class Logs(Seed):
    """Append searchable entries to the gitops audit log."""

    def seed_searchable_git_operation_logs(self) -> bool:
        """Commit fifty operations a log search can filter.

        Each subject is ``ops(searchable-N): searchable operation``. A second
        call writes nothing once those scopes are already in the history.

        Returns True when any commit was written.
        """
        gc = self._require_gitcrud()
        present = _scopes_present(gc)
        changed = False
        for num, scope in enumerate(searchable_log_scopes()):
            if scope in present:
                continue
            gc.put(
                _CLASS,
                _MARKER,
                {"n": num},
                SEED_ACTOR,
                message=build_message(
                    CommitContext(
                        ctype="ops",
                        scope=scope,
                        summary=_SUMMARY,
                        actor=SEED_ACTOR,
                    )
                ),
            )
            changed = True
        return changed


def _scopes_present(gc: GitCrud) -> set[str]:
    """Scopes already committed with the searchable summary."""
    found: set[str] = set()
    before: str | None = None
    wanted = set(searchable_log_scopes())
    while True:
        entries, before = read_log(gc, limit=500, before=before)
        for entry in entries:
            if entry.summary == _SUMMARY and entry.scope in wanted:
                found.add(entry.scope)
        if before is None or found == wanted:
            return found
