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

import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from dulwich.graph import can_fast_forward
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


@dataclass(frozen=True, slots=True)
class _Commit:
    """One commit as the walk reads it."""

    author: str
    files: tuple[str, ...]
    message: str
    sha: str
    timestamp: int


# Each repo's head and its commits newest first: diffing every commit's tree is what a walk costs
_WALKS: dict[str, tuple[ObjectID, tuple[_Commit, ...]]] = {}
_WALKS_LOCK = threading.Lock()


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
    # time). Cast so ty accepts our plain-bytes SHA the same way dulwich's own
    # internals do at runtime.
    try:
        walker = repo.get_walker(include=[cast(ObjectID, revision.encode())])
    except KeyError:
        return set()
    return {e.commit.id.decode() for e in walker}


def _commit(*, walked) -> _Commit:
    """The fields the log reads from one walk entry."""
    return _Commit(
        author=walked.commit.author.decode(errors="replace").split("<")[0].strip(),
        files=tuple(_changed_files(entry=walked)),
        message=walked.commit.message.decode(errors="replace"),
        sha=walked.commit.id.decode(),
        timestamp=walked.commit.commit_time,
    )


def _commits(*, key: str, repo: Repo) -> tuple[_Commit, ...]:
    """Every commit from head, newest first, walking only those this repo's last read did not see."""
    head = repo.head()
    with _WALKS_LOCK:
        known = _WALKS.get(key)
        if known and known[0] == head:
            return known[1]
        if known and not (_extends(head=head, known=known[0], repo=repo)):
            known = None
        walked = list(repo.get_walker(exclude=[known[0]] if known else [], include=[head]))
        # A merge interleaves its branch's older commits by date, so the walk starts over
        if known and any(len(entry.commit.parents) > 1 for entry in walked):
            known = None
            walked = list(repo.get_walker(include=[head]))
        fresh = tuple(_commit(walked=entry) for entry in walked)
        commits = (*fresh, *known[1]) if known else fresh
        _WALKS[key] = (head, commits)
        return commits


def _extends(*, head: ObjectID, known: ObjectID, repo: Repo) -> bool:
    """Whether head descends from the head a read walked before, so those commits still stand."""
    try:
        return can_fast_forward(c1=known, c2=head, repo=repo)
    except KeyError:
        return False


def _touches(*, commit: _Commit, paths: Sequence[str]) -> bool:
    """Whether the commit changed one of the paths or a file under one, as dulwich's path walk matches."""
    for path in paths:
        prefix = f"{path}/"
        for changed in commit.files:
            if (changed == path) or (changed.startswith(prefix)):
                return True
    return False


def read_log(
    crud: GitCrud,
    *,
    applied_revision: str | None = None,
    before: str | None = None,
    limit: int = 50,
    paths: Sequence[str] = (),
) -> tuple[list[LogEntry], str | None]:
    """Walk history newest-first. Returns (entries, next_before cursor or None).

    Given ``paths``, the walk keeps only the commits that touch one of them, so a
    cursor pages through those files' history alone.

    Raises UnknownCursorError if ``before`` is given but never matched a commit
    during the walk - an unknown/bad cursor must not look like end-of-history.
    """
    entries: list[LogEntry] = []
    # The walk reads the clone's refs and objects, so it takes the tree guard: a
    # refresh or a publish resets the tracked ref underneath it otherwise.
    with crud.reading(), Repo(str(crud.repo_path)) as repo:
        # The guard just made the local head current, so read it off the clone.
        if crud.repo.head_revision() is None:
            return [], None
        applied = _reachable(repo, applied_revision) if applied_revision else None
        skipping = before is not None
        exhausted = True
        if paths:
            commits = _commits(key=str(crud.repo_path), repo=repo)
            candidates = (
                (commit.sha, commit) for commit in commits if _touches(commit=commit, paths=paths)
            )
        else:
            # Unfiltered, the walk stops at the page and diffs nothing it skips
            candidates = ((walked.commit.id.decode(), walked) for walked in repo.get_walker())
        for sha, found in candidates:
            if skipping:
                if sha == before:
                    skipping = False
                continue
            if len(entries) >= limit:
                exhausted = False
                break
            commit = found if isinstance(found, _Commit) else _commit(walked=found)
            fields = _parse_message(author=commit.author, message=commit.message)
            files = list(commit.files)
            if applied is None:
                state = "committed"
            else:
                state = "applied" if sha in applied else "pending"
            entries.append(
                LogEntry(
                    sha=sha,
                    timestamp=commit.timestamp,
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
