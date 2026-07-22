#  Project:      dfe-engine
#  File:         gitcrud/log.py
#  Purpose:      Audit log over the deploy repo's git history
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Read the gitcrud audit log straight from git history (dulwich, no git CLI).

Every governed-ops mutation is ONE commit written to the commit standard
(type(scope): summary + DFE-* trailers), so the deploy repo's history IS the
audit log. This module walks it, parses each commit best-effort (hand commits
appear with conforming=False - the log must show ALL changes), resolves the
files touched back to class/resource via the registry, and derives git-only
state: committed, or applied/pending against a caller-supplied Argo-synced
revision (the engine never calls Argo).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from dulwich.objects import ObjectID
from dulwich.repo import Repo

from .commit_policy import ALLOWED_TYPES
from .engine import GitCrud

_SUBJECT_RE = re.compile(r"^(?P<ctype>[a-z_]+)\((?P<scope>[^)]*)\): (?P<summary>.+)$")
_TRAILER_RE = re.compile(r"^DFE-(?P<key>[A-Za-z-]+): (?P<value>.+)$")

GROUP_KEYS = ("scope", "actor", "type", "day")


class UnknownCursorError(ValueError):
    """Raised when a ``before`` cursor SHA never matched during the walk.

    Distinguishes a bad/unknown cursor from end-of-history (both would otherwise
    return the same ``([], None)`` shape).
    """


@dataclass(frozen=True)
class LogEntry:
    """One gitcrud operation, parsed from its commit."""

    sha: str
    timestamp: int  # commit time, epoch seconds
    ctype: str  # commit-standard type, "" when non-conforming
    scope: str
    summary: str  # parsed summary, or the whole subject when non-conforming
    actor: str  # DFE-Actor trailer, git author as fallback
    role: str
    action: str
    request_id: str
    files: list[str]
    resources: list[str]  # "cls/name" for files under a registered class dir
    conforming: bool
    state: str  # committed | applied | pending


def _parse_message(message: str, author: str) -> dict:
    lines = message.splitlines()
    subject = lines[0] if lines else ""
    fields = {
        "ctype": "",
        "scope": "",
        "summary": subject,
        "actor": author,
        "role": "",
        "action": "",
        "request_id": "",
        "conforming": False,
    }
    m = _SUBJECT_RE.match(subject)
    if m and m.group("ctype") in ALLOWED_TYPES:
        fields.update(
            ctype=m.group("ctype"),
            scope=m.group("scope"),
            summary=m.group("summary"),
            conforming=True,
        )
    for line in lines[1:]:
        t = _TRAILER_RE.match(line.strip())
        if not t:
            continue
        key = t.group("key").lower().replace("-", "_")
        if key == "actor":
            fields["actor"] = t.group("value")
        elif key in ("role", "action", "request_id"):
            fields[key] = t.group("value")
    return fields


def _resources_for(files: list[str], crud: GitCrud) -> list[str]:
    out: list[str] = []
    for path in files:
        for cls in crud.registry.all():
            prefix = f"{cls.directory}/"
            if path.startswith(prefix) and path.endswith(cls.suffix):
                name = path[len(prefix) : -len(cls.suffix)]
                out.append(f"{cls.name}/{name}")
                break
    return out


def _changed_files(entry) -> list[str]:
    files: list[str] = []
    for change in entry.changes():
        tc = change if not isinstance(change, list) else change[0]
        new_path = tc.new.path if tc.new is not None else None
        old_path = tc.old.path if tc.old is not None else None
        p = new_path or old_path
        if p:
            files.append(p.decode())
    return sorted(set(files))


def _reachable(repo: Repo, revision: str) -> set[str]:
    # dulwich's ObjectID is a NewType over bytes (nominal-only at type-check
    # time); cast so ty accepts our plain-bytes SHA the same way dulwich's own
    # internals do at runtime.
    try:
        walker = repo.get_walker(include=[cast(ObjectID, revision.encode())])
    except KeyError:
        return set()
    return {e.commit.id.decode() for e in walker}


def read_log(
    crud: GitCrud,
    *,
    limit: int = 50,
    before: str | None = None,
    applied_revision: str | None = None,
) -> tuple[list[LogEntry], str | None]:
    """Walk history newest-first; returns (entries, next_before cursor or None).

    Raises UnknownCursorError if ``before`` is given but never matched a commit
    during the walk - an unknown/bad cursor must not look like end-of-history.
    """
    if crud.head_revision() is None:
        return [], None
    entries: list[LogEntry] = []
    with Repo(str(crud.repo_path)) as repo:
        applied = _reachable(repo, applied_revision) if applied_revision else None
        skipping = before is not None
        exhausted = True
        for walked in repo.get_walker():
            sha = walked.commit.id.decode()
            if skipping:
                if sha == before:
                    skipping = False
                continue
            if len(entries) >= limit:
                exhausted = False
                break
            message = walked.commit.message.decode(errors="replace")
            author = walked.commit.author.decode(errors="replace").split("<")[0].strip()
            fields = _parse_message(message, author)
            files = _changed_files(walked)
            if applied is None:
                state = "committed"
            else:
                state = "applied" if sha in applied else "pending"
            entries.append(
                LogEntry(
                    sha=sha,
                    timestamp=walked.commit.commit_time,
                    files=files,
                    resources=_resources_for(files, crud),
                    state=state,
                    **fields,
                )
            )
        if skipping:
            raise UnknownCursorError(f"before cursor never matched a commit: {before!r}")
    next_before = entries[-1].sha if entries and not exhausted else None
    return entries, next_before


def group_log(entries: list[LogEntry], by: str) -> list[dict]:
    """Bucket entries (newest-first input) -> [{key, count, latest}]."""
    if by not in GROUP_KEYS:
        raise ValueError(f"group_by must be one of {GROUP_KEYS}, got {by!r}")
    buckets: dict[str, dict] = {}
    for e in entries:
        if by == "day":
            key = datetime.fromtimestamp(e.timestamp, tz=UTC).strftime("%Y-%m-%d")
        elif by == "type":
            key = e.ctype or "(non-conforming)"
        elif by == "actor":
            key = e.actor
        else:
            key = e.scope or "(none)"
        b = buckets.setdefault(key, {"key": key, "count": 0, "latest": e})
        b["count"] += 1
    return list(buckets.values())
