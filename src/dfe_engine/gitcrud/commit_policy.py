#  Project:      dfe-engine
#  File:         gitcrud/commit_policy.py
#  Purpose:      Enforce the DFE GitOps Commit Standard in code
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Build + validate Governed Ops commit messages, and resolve direct-vs-PR mode.

Callers never hand-write commit messages; they pass a CommitContext and this module
produces a conforming message (type(scope): summary <=50, ASCII, [skip ci], audit
trailers). See docs/GITOPS-COMMIT-STANDARD.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dfe_engine.settings import is_dev_posture

# Gitops-operational commit types (NOT release fix/feat - deploy repo has no
# semantic-release). See the standard.
ALLOWED_TYPES = frozenset({"cfg", "hunt", "rbac", "action", "ops", "schema", "seed"})

# Map a resource CLASS prefix -> its commit type.
_CLASS_TYPE = {
    "helmvars": "cfg",
    "governance": "rbac",  # rbac/actions/policies; action invokes override to "action"
    "hunts": "hunt",
    "datamodel": "schema",
}

_SUBJECT_MAX = 50


class CommitPolicyError(ValueError):
    """Raised when a commit message or change violates the standard."""


def type_for_class(rbac_class: str) -> str:
    """Commit type for a resource class (defaults to 'cfg')."""
    return _CLASS_TYPE.get(rbac_class, "cfg")


@dataclass
class CommitContext:
    """Everything needed to build one standard commit + its audit trailers."""

    ctype: str  # one of ALLOWED_TYPES
    scope: str  # resource name / action name
    summary: str  # imperative, lowercase-ish, no trailing period
    actor: str
    role: str = ""
    action: str = ""  # the defined-action name, if via an action
    request_id: str = ""
    base_revision: str = ""
    audit_id: str = ""
    trailers_extra: dict[str, str] = field(default_factory=dict)


def _is_ascii(text: str) -> bool:
    return all(ord(c) < 128 for c in text)


def build_message(ctx: CommitContext) -> str:
    """Render the conforming commit message (subject + trailers + [skip ci])."""
    subject = f"{ctx.ctype}({ctx.scope}): {ctx.summary}"
    validate_subject(subject)

    trailers: list[str] = [f"DFE-Actor: {ctx.actor}"]
    if ctx.role:
        trailers.append(f"DFE-Role: {ctx.role}")
    if ctx.action:
        trailers.append(f"DFE-Action: {ctx.action}")
    if ctx.request_id:
        trailers.append(f"DFE-Request-Id: {ctx.request_id}")
    if ctx.base_revision:
        trailers.append(f"DFE-Base-Revision: {ctx.base_revision}")
    if ctx.audit_id:
        trailers.append(f"DFE-Audit-Id: {ctx.audit_id}")
    for k, v in ctx.trailers_extra.items():
        trailers.append(f"{k}: {v}")

    body = "\n".join(trailers)
    return f"{subject}\n\n{body}\n\n[skip ci]"


def validate_subject(subject: str) -> None:
    """ASCII-only, <=50 chars, an allowed type prefix."""
    if not _is_ascii(subject):
        raise CommitPolicyError(f"non-ASCII subject: {subject!r}")
    if len(subject) > _SUBJECT_MAX:
        raise CommitPolicyError(f"subject > {_SUBJECT_MAX} chars: {subject!r}")
    ctype = subject.split("(", 1)[0].split(":", 1)[0]
    if ctype not in ALLOWED_TYPES:
        raise CommitPolicyError(f"type {ctype!r} not in {sorted(ALLOWED_TYPES)}")


def validate_change(path: str, value: object) -> None:
    """Reject immutability/self-heal hazards in a var write (the standard).

    - image/chart refs must be pinned: no ``latest``, no untagged ref.
    - controller-owned fields must not be tracked under self-heal (KEDA owns
      replicas -> set keda.min/maxReplicas, not replicaCount).
    """
    leaf = path.rsplit(".", 1)[-1]
    if leaf in {"tag", "image"} and isinstance(value, str):
        v = value.strip()
        if v == "" or v.endswith(":latest") or v == "latest":
            raise CommitPolicyError(f"unpinned/floating image ref at {path}: {value!r}")
    if path == "replicaCount" or path.endswith(".replicaCount"):
        raise CommitPolicyError(
            f"{path} is controller-owned (KEDA); set keda.min/maxReplicas instead"
        )


def resolve_mode(
    *,
    environment: str,
    rbac_class: str,
    protected: bool = False,
    require_pr: bool = False,
    auto_merge: bool = False,
) -> str:
    """'pr' for production posture / protected / governance / explicit; else 'direct'.

    PRs for production-controlling changes, direct-commit for low-risk - enforced at
    the git level (the standard). Production posture is anything DFE_ENV does not
    declare as dev (is_dev_posture), matching auto_merge.gate(). auto_merge=True
    (the solo/dev fast path) force-directs EVERYTHING - the caller is responsible
    for gating + loud warnings (see gitcrud/auto_merge.py).
    """
    if auto_merge:
        return "direct"
    if require_pr or protected or rbac_class == "governance" or not is_dev_posture(environment):
        return "pr"
    return "direct"
