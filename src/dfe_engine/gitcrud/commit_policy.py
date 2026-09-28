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
trailers). See docs/control-plane/gitops-commit-standard.md.
"""

import re
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
    "schema": "schema",
}

SUBJECT_MAX = 50

# A resource name is BOTH a file-path component (values/<name>.yaml) and a commit
# subject input (scope). Keep it boring: letters/digits/dot/underscore/dash only.
# ``fullmatch`` (not ``match``) anchors the whole string, so a trailing newline is
# rejected -- ``$`` would otherwise match just before it and let a name smuggle a
# ``DFE-Role:``/``DFE-Action:`` trailer line into the commit body.
# Not store_names.VALID_NAME: a deploy-repo name has no length cap or leading-character rule.
_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


class CommitPolicyError(ValueError):
    """Raised when a commit message or change violates the standard."""


def validate_name(name: str) -> None:
    """Reject a resource name that could traverse the tree or inject a trailer.

    Defence in depth for the gitcrud write path: a ``/`` would escape the class
    ``values/`` directory, ``..`` would climb out of it, and a newline could forge
    ``DFE-Role``/``DFE-Action`` audit trailers in the commit message. Anything
    outside ``[A-Za-z0-9._-]`` -- or containing ``..`` -- is refused.
    """
    if not name or ".." in name or not _NAME_RE.fullmatch(name):
        raise CommitPolicyError(f"invalid resource name: {name!r}")


def validate_resource_path(name: str) -> None:
    """The same rule for a class whose names carry directory segments.

    Every segment goes through :func:`validate_name`, so the one chokepoint still
    decides what a name may contain; only the separator is permitted extra.
    """
    if not name or name.startswith("/") or name.endswith("/") or "//" in name:
        raise CommitPolicyError(f"invalid resource name: {name!r}")
    for segment in name.split("/"):
        validate_name(segment)


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


def fit_subject(ctype: str, scope: str, summary: str) -> str:
    """A ``type(scope): summary`` subject trimmed to fit ``SUBJECT_MAX``.

    The scope names the resource being changed and the summary only describes
    the edit, so the summary gives way first and the scope survives whole; the
    commit's file list records the change exactly either way. Only a scope that
    would overrun the budget on its own is elided.

    Long scopes are the normal case, not the exotic one: a per-instance overlay
    is named ``{service}-{instance}-values``, which passes 31 characters as soon
    as an app runs more than one instance.
    """
    head = f"{ctype}({scope})"
    room = SUBJECT_MAX - len(head) - len(": ")
    if room > 0:
        return f"{head}: {summary[:room].rstrip()}"
    if len(head) <= SUBJECT_MAX:
        return head
    keep = SUBJECT_MAX - len(ctype) - len("()")
    return f"{ctype}({scope[: max(keep, 1)]})"


def build_message(ctx: CommitContext) -> str:
    """Render the conforming commit message (subject + trailers + [skip ci])."""
    subject = fit_subject(ctx.ctype, ctx.scope, ctx.summary)
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
    """ASCII-only, single-line, <=50 chars, an allowed type prefix."""
    if not _is_ascii(subject):
        raise CommitPolicyError(f"non-ASCII subject: {subject!r}")
    # A newline in the subject would split into the commit body and could forge
    # DFE-* audit trailers -- reject CR/LF outright.
    if "\n" in subject or "\r" in subject:
        raise CommitPolicyError(f"newline in subject: {subject!r}")
    if len(subject) > SUBJECT_MAX:
        raise CommitPolicyError(f"subject > {SUBJECT_MAX} chars: {subject!r}")
    ctype = subject.split("(", 1)[0].split(":", 1)[0]
    if ctype not in ALLOWED_TYPES:
        raise CommitPolicyError(f"type {ctype!r} not in {sorted(ALLOWED_TYPES)}")


def validate_change(path: str, value: object, doc: dict | None = None) -> None:
    """Reject immutability/self-heal hazards in a var write (the standard).

    - image/chart refs must be pinned: no ``latest``, no untagged ref.
    - controller-owned fields must not be tracked under self-heal (KEDA owns
      replicas -> set keda.min/maxReplicas, not replicaCount).

    ``doc`` is the document the write lands in. A document that explicitly sets
    ``keda.enabled: false`` has no controller owning the replica count, and
    ``replicaCount`` is then the only way to set it, so it is allowed there. With
    no document the field stays refused: an unset flag means the chart default,
    which is not readable from here.
    """
    leaf = path.rsplit(".", 1)[-1]
    if leaf in {"tag", "image"} and isinstance(value, str):
        v = value.strip()
        if v == "" or v.endswith(":latest") or v == "latest":
            raise CommitPolicyError(f"unpinned/floating image ref at {path}: {value!r}")
    if (path == "replicaCount" or path.endswith(".replicaCount")) and not _keda_disabled(doc):
        raise CommitPolicyError(
            f"{path} is controller-owned (KEDA); set keda.min/maxReplicas instead, "
            "or disable KEDA in the same write"
        )


def _keda_disabled(doc: dict | None) -> bool:
    """Whether the document hands the replica count back to the deployment."""
    if not isinstance(doc, dict):
        return False
    keda = doc.get("keda")
    return isinstance(keda, dict) and keda.get("enabled") is False


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
