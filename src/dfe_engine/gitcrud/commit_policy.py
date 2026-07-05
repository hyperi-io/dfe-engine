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
# semantic-release). See the standard. 'lifecycle' is the start/stop/pause state
# dial (governance/lifecycle.py) - kept a first-class type so its audit entries
# read conforming and group distinctly rather than mis-parsing as non-conforming.
ALLOWED_TYPES = frozenset({"cfg", "hunt", "rbac", "action", "ops", "schema", "seed", "lifecycle"})

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


def _fit_subject(ctype: str, scope: str, summary: str, limit: int = _SUBJECT_MAX) -> str:
    """Compose ``type(scope): summary`` trimmed to fit ``limit`` chars.

    Ordinary file-name + var-leaf combos routinely exceed 50 (e.g.
    ``cfg(receiver-default): set clickhouse_max_connections`` is 53), so we
    budget-truncate rather than reject a legitimate write - raising here 500'd the
    helm PUT (the standard wants the write to land, just conforming). The type is
    never trimmed (short ALLOWED type, and read_log keys off it); summary yields
    first, then scope, and summary keeps >=1 char so read_log's subject regex
    (``: (?P<summary>.+)``) still matches (an empty summary reads non-conforming).
    """
    fixed = len(ctype) + len("(): ")
    budget = max(limit - fixed, 2)  # >=1 char each for scope and summary
    max_scope = budget - 1
    if len(scope) > max_scope:
        scope = scope[:max_scope]
    summary = summary[: budget - len(scope)]
    if not summary:
        summary = "-"
    return f"{ctype}({scope}): {summary}"


def build_message(ctx: CommitContext) -> str:
    """Render the conforming commit message (subject + trailers + [skip ci])."""
    subject = _fit_subject(ctx.ctype, ctx.scope, ctx.summary)
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
    if not subject.isascii():
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
