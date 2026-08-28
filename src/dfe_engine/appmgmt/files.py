#  Project:      dfe-engine
#  File:         appmgmt/files.py
#  Purpose:      The files an app consumes, carried inside its values overlay
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""CRUD for the files an app reads off disk.

Content lives in the instance's values overlay rather than as loose files in the
deploy repo, because Helm cannot read a raw file out of an Argo ``$values`` source -
a chart-rendered ConfigMap can only contain what is already in the values.

The stored shape is a LIST of ``{name, content}`` rather than a filename-keyed map:
gitcrud flattens documents to dot-paths, and a filename's own dot would make
``transformFiles.000_parse.vrl`` ambiguous with a nested mapping.

These are pure document operations. Committing them is the router's job, so the
write goes through the same policy and review routing as every other helm-var change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from dfe_engine.gitcrud.engine import get_path, set_path
from dfe_engine.yaml_utils import literal_block

from .catalogue import ConsumedFileSet

_NAME_FIELD = "name"
_CONTENT_FIELD = "content"

# A plain basename. Excludes '/' and '..' so a filename can never escape the
# directory the chart mounts it into.
_FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class InvalidFilenameError(ValueError):
    """Raised when a filename is unsafe or carries an extension the app cannot read."""


class FileNotInSetError(KeyError):
    """Raised when the named file is not present in the set."""


@dataclass(frozen=True, slots=True)
class AppFile:
    """One file an app consumes."""

    name: str
    content: str
    language: str

    @property
    def size_bytes(self) -> int:
        return len(self.content.encode("utf-8"))


def validate_filename(file_set: ConsumedFileSet, name: str) -> None:
    """Reject a filename that is unsafe, or that the app would not read."""
    if ".." in name or not _FILENAME_RE.fullmatch(name):
        raise InvalidFilenameError(
            f"invalid filename {name!r}: expected a plain basename of "
            "alphanumerics, '.', '_' and '-'"
        )
    if not name.endswith(file_set.suffixes):
        allowed = ", ".join(file_set.suffixes)
        raise InvalidFilenameError(
            f"{name!r} does not end in an extension this app reads (expected: {allowed})"
        )


def _entries(doc: dict, file_set: ConsumedFileSet) -> list[dict]:
    raw = get_path(doc, file_set.values_path, default=None)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"{file_set.values_path} holds {type(raw).__name__}, expected a list of "
            "{name, content} entries"
        )
    return [e for e in raw if isinstance(e, dict) and _NAME_FIELD in e]


def list_files(doc: dict, file_set: ConsumedFileSet) -> list[AppFile]:
    """Every file in the set, in the order the overlay stores them."""
    return [
        AppFile(
            name=str(entry[_NAME_FIELD]),
            content=str(entry.get(_CONTENT_FIELD, "")),
            language=file_set.language,
        )
        for entry in _entries(doc, file_set)
    ]


def read_file(doc: dict, file_set: ConsumedFileSet, name: str) -> AppFile:
    """One file's content."""
    for candidate in list_files(doc, file_set):
        if candidate.name == name:
            return candidate
    raise FileNotInSetError(name)


def upsert_file(doc: dict, file_set: ConsumedFileSet, name: str, content: str) -> bool:
    """Add or replace a file in the set. Mutates ``doc``. Returns whether it changed.

    New files are appended rather than sorted in, because the order the overlay
    stores them is the author's; dfe-transform-vrl concatenates by filename
    regardless, so ordering here is presentation, not behaviour.
    """
    validate_filename(file_set, name)
    entries = _entries(doc, file_set)
    body = literal_block(content)
    for entry in entries:
        if entry[_NAME_FIELD] == name:
            if str(entry.get(_CONTENT_FIELD, "")) == str(body):
                return False
            entry[_CONTENT_FIELD] = body
            set_path(doc, file_set.values_path, entries)
            return True
    entries.append({_NAME_FIELD: name, _CONTENT_FIELD: body})
    set_path(doc, file_set.values_path, entries)
    return True


def delete_file(doc: dict, file_set: ConsumedFileSet, name: str) -> bool:
    """Remove a file from the set. Mutates ``doc``. Returns whether it changed."""
    entries = _entries(doc, file_set)
    remaining = [e for e in entries if e[_NAME_FIELD] != name]
    if len(remaining) == len(entries):
        raise FileNotInSetError(name)
    set_path(doc, file_set.values_path, remaining)
    return True
