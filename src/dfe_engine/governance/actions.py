#  Project:      dfe-engine
#  File:         governance/actions.py
#  Purpose:      Defined-action store + atomic invoke (Governed Ops Tier-2)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Curated defined actions: CRUD their YAML in gitops, and invoke them atomically.

An action bundles var changes behind one RBAC handle. Invoking applies ALL changes
in ONE commit (atomic - a protected-var violation fails the whole action), honours
the protected-var policy, and supports dry-run (returns the diff without writing).
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass, field
from typing import Any

from dfe_engine.gitcrud import GitCrud, ResourceNotFoundError, get_path, set_path
from dfe_engine.gitcrud.commit_policy import CommitContext, build_message, validate_change
from dfe_engine.gitops.repo import PublishResult

from .models import ActionDef
from .policies import PolicyStore

_ACTION_CLASS = "actions"


def _action_message(name: str, verb: str, actor: str) -> str:
    """Conforming commit message for an action op ('action' is an ALLOWED type).

    Routes through build_message so the DFE-Actor trailer is present (else the
    audit log falls back to the git author 'dfe-engine'); ``define`` / ``invoke``
    / ``delete`` name the op in the summary.
    """
    return build_message(CommitContext(ctype="action", scope=name, summary=verb, actor=actor))


class ActionForbiddenError(PermissionError):
    """Raised when an action tries to do something actions are not allowed to do."""


@dataclass
class InvokeResult:
    """Outcome of invoking an action."""

    dry_run: bool
    changed: bool
    commit_sha: str | None
    diff: list[dict[str, Any]] = field(default_factory=list)


class ActionStore:
    """CRUD defined actions (gitops ``actions`` class) and invoke them."""

    def __init__(self, crud: GitCrud) -> None:
        self._crud = crud

    def list(self) -> builtins.list[str]:
        return self._crud.list(_ACTION_CLASS)

    def get(self, name: str) -> ActionDef:
        return ActionDef.model_validate(self._crud.get(_ACTION_CLASS, name))

    def save(self, action: ActionDef, actor: str) -> PublishResult:
        return self._crud.put(
            _ACTION_CLASS,
            action.name,
            action.model_dump(),
            actor,
            message=_action_message(action.name, "define", actor),
        )

    def delete(self, name: str, actor: str) -> PublishResult:
        return self._crud.delete(
            _ACTION_CLASS, name, actor, message=_action_message(name, "delete", actor)
        )

    def invoke(
        self,
        name: str,
        actor: str,
        *,
        policy: PolicyStore | None = None,
        dry_run: bool = False,
        override: bool = False,
    ) -> InvokeResult:
        """Apply all of an action's changes atomically (or preview with dry_run)."""
        action = self.get(name)
        docs: dict[tuple[str, str], dict] = {}
        diff: list[dict[str, Any]] = []

        for ch in action.changes:
            # Security: actions are for operational dials, NOT RBAC. An action that
            # could write the governance class is a privilege-escalation path, so
            # forbid it outright - RBAC changes go through admin Tier-1 only.
            if self._crud.resource_class(ch.cls).rbac_prefix == "governance":
                raise ActionForbiddenError(
                    f"action '{name}' may not change the governance class ({ch.cls})"
                )
            # Same commit-policy validators as the direct path (no latest/replicaCount
            # sneaking in via an action).
            validate_change(ch.path, ch.value)
            key = (ch.cls, ch.name)
            if key not in docs:
                try:
                    docs[key] = self._crud.get(ch.cls, ch.name)
                except ResourceNotFoundError:
                    docs[key] = {}
            if policy is not None:
                # Atomic: a protected-var violation aborts the WHOLE action.
                policy.enforce(ch.cls, ch.name, ch.path, override=override)
            diff.append(
                {
                    "cls": ch.cls,
                    "name": ch.name,
                    "path": ch.path,
                    "old": get_path(docs[key], ch.path),
                    "new": ch.value,
                }
            )
            set_path(docs[key], ch.path, ch.value)

        if dry_run:
            return InvokeResult(dry_run=True, changed=False, commit_sha=None, diff=diff)

        items = [(cls, nm, doc) for (cls, nm), doc in docs.items()]
        res = self._crud.put_many(items, actor, _action_message(action.name, "invoke", actor))
        return InvokeResult(
            dry_run=False, changed=res.changed, commit_sha=res.commit_sha, diff=diff
        )
