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

import builtins
import copy
import functools
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from scalo.logger import logger

from dfe_engine.appmgmt import contract
from dfe_engine.gitcrud import GitCrud, ResourceNotFoundError, get_path, set_path
from dfe_engine.gitcrud.commit_policy import CommitPolicyError, validate_change
from dfe_engine.gitcrud.registry import UnknownResourceClassError
from dfe_engine.gitops.repo import PublishResult

from .models import ActionDef, VarChange, param_ref
from .policies import PolicyStore, ProtectedVarError

ACTION_CLASS = "actions"
"""The gitops class action definitions are stored in."""

CREDENTIAL_REFUSAL = (
    "an action definition is stored in the deploy repo's history, so it may not carry "
    "a credential: set credentials through the vars routes (PUT "
    "/api/v1/apps/{service}/{instance}/config or /api/v1/helm/files/{name}/vars/{path})"
)
"""Why a change that would store a credential is refused, in the words the caller reads."""


@functools.cache
def _warn_stored_credential(name: str, targets: tuple[str, ...]) -> None:
    """Log once per process that a stored definition carries a credential.

    Cached on its arguments, so a definition read on every request warns once.
    ``_warn_stored_credential.cache_clear()`` lets it warn again.
    """
    logger.warning(
        "Action definition stores a credential; move it to the vars routes",
        action=name,
        changes=list(targets),
    )


class ActionForbiddenError(PermissionError):
    """Raised when an action tries to do something actions are not allowed to do."""


class CredentialInActionError(ValueError):
    """Raised when an action definition would store a credential in the deploy repo."""


class InvalidParamsError(ValueError):
    """Raised when supplied invoke params violate the action's declared constraints."""


def resolve_params(action: ActionDef, params: dict[str, Any] | None) -> dict[str, Any]:
    """Validate supplied params against the declared constraints, applying defaults.

    Raises InvalidParamsError on an unknown name, a missing no-default param, or
    a value outside its closed constraint. Returns the full resolved mapping.
    """
    given = dict(params or {})
    unknown = sorted(set(given) - set(action.params))
    if unknown:
        raise InvalidParamsError(f"unknown params: {', '.join(unknown)}")
    resolved: dict[str, Any] = {}
    for pname, spec in action.params.items():
        if pname in given:
            value = given[pname]
        elif spec.default is not None:
            value = spec.default
        else:
            raise InvalidParamsError(f"missing required param '{pname}'")
        try:
            resolved[pname] = spec.check(pname, value)
        except ValueError as exc:
            raise InvalidParamsError(str(exc)) from exc
    return resolved


def _substitute(value: Any, resolved: dict[str, Any]) -> Any:
    """Whole-value substitution of a param reference; literals pass through.

    The define-time model validator guarantees the reference resolves and any
    map covers every enum value, so lookups here cannot miss.
    """
    ref = param_ref(value)
    if ref is None:
        return value
    pname, mapping = ref
    pvalue = resolved[pname]
    return mapping[pvalue] if mapping is not None else pvalue


def _masked(target: dict, path: str, value: Any) -> Any:
    """``value`` as a reader sees it at a credential ``path``.

    Its own credential fields are masked where it has them; otherwise the path is
    what makes it a credential, and the whole value is masked.
    """
    shown = contract.shown_var(target, path, value)
    if shown != value or value is None or value == "":
        return shown
    return contract.REDACTED


def _mask_param(doc: dict, pname: str) -> None:
    """Mask an enum param whose values are credentials, keeping every map on it valid.

    Each value gets its own placeholder, so a map keyed on the param still covers
    exactly its values and the definition still loads.
    """
    spec = (doc.get("params") or {}).get(pname)
    if not isinstance(spec, dict) or not isinstance(spec.get("values"), list):
        return
    table = {value: f"{contract.REDACTED}{i}" for i, value in enumerate(spec["values"])}
    spec["values"] = [table[value] for value in spec["values"]]
    if spec.get("default") in table:
        spec["default"] = table[spec["default"]]
    for change in doc.get("changes") or ():
        ref = param_ref(change.get("value"))
        if ref is not None and ref[0] == pname and ref[1] is not None:
            change["value"] = {
                "$param": pname,
                "map": {table.get(key, key): item for key, item in ref[1].items()},
            }


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
        return self._crud.list(ACTION_CLASS)

    def get(self, name: str) -> ActionDef:
        """The stored definition as written, credentials included: for invoking it."""
        return ActionDef.model_validate(self._crud.get(ACTION_CLASS, name))

    def get_shown(self, name: str) -> ActionDef:
        """The stored definition for a reader, any credential it carries masked."""
        return ActionDef.model_validate(self.shown_doc(name, self._crud.get(ACTION_CLASS, name)))

    def _target(self, change: VarChange) -> dict:
        """The document a change writes into, or nothing where there is none yet."""
        try:
            return self._crud.get(change.cls, change.name)
        except (ResourceNotFoundError, UnknownResourceClassError):
            return {}

    def _credential_change(self, change: VarChange) -> bool:
        """Whether ``change`` would store a credential, by its path or by any value it can take."""
        target = self._target(change)
        if contract.credential_var(target, change.path):
            return True
        ref = param_ref(change.value)
        if ref is None:
            candidates = [change.value]
        elif ref[1] is not None:
            candidates = list(ref[1].values())
        else:
            candidates = []
        return any(contract.credential_var(target, change.path, v) for v in candidates)

    def credential_changes(self, action: ActionDef) -> builtins.list[str]:
        """Each change of ``action`` that would store a credential, as ``cls/name:path``."""
        return [
            f"{ch.cls}/{ch.name}:{ch.path}" for ch in action.changes if self._credential_change(ch)
        ]

    def refuse_credentials(self, action: ActionDef) -> None:
        """Raise :class:`CredentialInActionError` where ``action`` would store one.

        Raises:
            CredentialInActionError: A change targets a credential.
        """
        found = self.credential_changes(action)
        if found:
            raise CredentialInActionError(
                f"action {action.name!r} changes {', '.join(found)}: {CREDENTIAL_REFUSAL}"
            )

    def shown_doc(self, name: str, doc: dict) -> dict:
        """A stored definition with every credential it carries masked.

        A definition stored before credentials were refused still invokes with
        what it holds; only what is read back is masked, and each such definition
        is logged once so an operator can move it to the vars routes.
        """
        try:
            action = ActionDef.model_validate(doc)
        except ValidationError:
            return contract.redact_resource(doc)
        flagged = [i for i, ch in enumerate(action.changes) if self._credential_change(ch)]
        if not flagged:
            return doc
        _warn_stored_credential(
            name, tuple(f"{action.changes[i].cls}/{action.changes[i].name}" for i in flagged)
        )
        shown = copy.deepcopy(doc)
        masked_params: set[str] = set()
        for i in flagged:
            change = action.changes[i]
            target = self._target(change)
            ref = param_ref(change.value)
            if ref is None:
                shown["changes"][i]["value"] = _masked(target, change.path, change.value)
            elif ref[1] is not None:
                shown["changes"][i]["value"] = {
                    "$param": ref[0],
                    "map": {k: _masked(target, change.path, v) for k, v in ref[1].items()},
                }
            else:
                masked_params.add(ref[0])
        for pname in masked_params:
            _mask_param(shown, pname)
        return shown

    def save(self, action: ActionDef, actor: str, branch: str = "") -> PublishResult:
        return self._crud.put(
            ACTION_CLASS,
            action.name,
            action.model_dump(),
            actor,
            message=f"action({action.name}): define by {actor}",
            branch=branch,
        )

    def delete(self, name: str, actor: str, branch: str = "") -> PublishResult:
        return self._crud.delete(ACTION_CLASS, name, actor, branch=branch)

    def _walk(
        self,
        action: ActionDef,
        *,
        policy: PolicyStore | None,
        override: bool,
        collect: bool,
        resolved: dict[str, Any] | None = None,
    ) -> tuple[dict[tuple[str, str], dict], builtins.list[dict[str, Any]], builtins.list[str]]:
        """Resolve an action's changes into (docs, diff, errors).

        collect=False is the invoke path: the first violation raises, keeping the
        action atomic. collect=True is the validate path: every violation is
        reported in one pass so an author fixes the lot in one round trip.
        ``resolved`` carries the checked param values; substitution happens
        BEFORE commit-policy validation so the resolved literal is what gets
        validated.
        """
        docs: dict[tuple[str, str], dict] = {}
        diff: list[dict[str, Any]] = []
        errors: list[str] = []

        for ch in action.changes:
            value = _substitute(ch.value, resolved or {})
            try:
                # Security: actions are for operational dials, NOT RBAC. An action
                # that could write the governance class is a privilege-escalation
                # path, so forbid it outright - RBAC changes go through admin
                # Tier-1 only.
                if self._crud.resource_class(ch.cls).rbac_prefix == "governance":
                    raise ActionForbiddenError(
                        f"action '{action.name}' may not change the governance class ({ch.cls})"
                    )
                # Same commit-policy validators as the direct path (no
                # latest/replicaCount sneaking in via an action).
                validate_change(ch.path, value)
                if policy is not None:
                    # Atomic: a protected-var violation aborts the WHOLE action.
                    policy.enforce(ch.cls, ch.name, ch.path, override=override)
            except (
                UnknownResourceClassError,
                ActionForbiddenError,
                CommitPolicyError,
                ProtectedVarError,
            ) as exc:
                if not collect:
                    raise
                msg = str(exc) if str(exc) else f"unknown resource class '{ch.cls}'"
                errors.append(f"{ch.cls}/{ch.name}:{ch.path}: {msg}")
                continue
            key = (ch.cls, ch.name)
            if key not in docs:
                try:
                    docs[key] = self._crud.get(ch.cls, ch.name)
                except ResourceNotFoundError:
                    docs[key] = {}
            doc = docs[key]
            # A value copied from a masked listing keeps the credential stored there.
            try:
                value = contract.restore_masked_at(doc, ch.path, value)
            except contract.MaskedValueError as exc:
                if not collect:
                    raise
                errors.append(f"{ch.cls}/{ch.name}:{ch.path}: {exc}")
                continue
            # The diff is returned to the caller, so a credential in it is masked.
            diff.append(
                {
                    "cls": ch.cls,
                    "name": ch.name,
                    "path": ch.path,
                    "old": contract.shown_var(doc, ch.path, get_path(doc, ch.path)),
                    "new": contract.shown_var(doc, ch.path, value),
                }
            )
            set_path(doc, ch.path, value)
        return docs, diff, errors

    def preview(
        self,
        action: ActionDef,
        *,
        policy: PolicyStore | None = None,
        override: bool = False,
    ) -> tuple[builtins.list[dict[str, Any]], builtins.list[str]]:
        """Validate an UNSAVED action definition; nothing is written.

        Returns (diff, errors): the diff the action would apply, plus every
        violation found - unknown class, governance-class target, commit-policy
        ban, protected var without override, a credential. Params resolve to their default or
        representative value (closed constraints make one always available), so
        a definition validates without a caller-supplied invocation - and every
        enum map BRANCH is commit-policy checked, not just the representative,
        so no detent can hide a banned value until invoke time.
        """
        representatives = {n: s.representative() for n, s in action.params.items()}
        _, diff, errors = self._walk(
            action, policy=policy, override=override, collect=True, resolved=representatives
        )
        for ch in action.changes:
            ref = param_ref(ch.value)
            if ref is None or ref[1] is None:
                continue
            for enum_value, mapped in ref[1].items():
                try:
                    validate_change(ch.path, mapped)
                except CommitPolicyError as exc:
                    errors.append(f"{ch.cls}/{ch.name}:{ch.path}: map branch {enum_value!r}: {exc}")
        errors.extend(f"{found}: {CREDENTIAL_REFUSAL}" for found in self.credential_changes(action))
        return diff, errors

    def invoke(
        self,
        name: str,
        actor: str,
        *,
        params: dict[str, Any] | None = None,
        policy: PolicyStore | None = None,
        dry_run: bool = False,
        override: bool = False,
        branch: str = "",
    ) -> InvokeResult:
        """Apply all of an action's changes atomically (or preview with dry_run).

        ``params`` are checked against the action's declared constraints
        (InvalidParamsError on violation) and substituted into change values
        before any validation or write.
        """
        action = self.get(name)
        resolved = resolve_params(action, params)
        docs, diff, _ = self._walk(
            action, policy=policy, override=override, collect=False, resolved=resolved
        )

        if dry_run:
            return InvokeResult(dry_run=True, changed=False, commit_sha=None, diff=diff)

        items = [(cls, nm, doc) for (cls, nm), doc in docs.items()]
        res = self._crud.put_many(
            items, actor, f"action({action.name}): invoke by {actor}", branch=branch
        )
        return InvokeResult(
            dry_run=False, changed=res.changed, commit_sha=res.commit_sha, diff=diff
        )
