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
``fileSets.transforms.files.000_parse.vrl`` ambiguous with a nested mapping.

A set the app reads table by table (``entries_path``) is named in the app's own
config as its files are written, because the config file takes no template and
nothing downstream derives the entries.

These are pure document operations. Committing them is the router's job, so the
write goes through the same policy and review routing as every other helm-var change.
"""

import re
from dataclasses import dataclass

from scalo.logger import logger

from dfe_engine.gitcrud.engine import get_path, set_path
from dfe_engine.yaml_utils import literal_block

from .catalogue import ConsumedFileSet

_NAME_FIELD = "name"
_CONTENT_FIELD = "content"

# A plain basename. Excludes '/' and '..' so a filename can never escape the
# directory the chart mounts it into.
_FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


# YAML carries no C0 control character except tab and newline, so content holding
# one produces a document the emitter writes and the parser then refuses, leaving
# an overlay nothing can read or repair.
_FORBIDDEN_CONTENT = frozenset(chr(c) for c in range(0x20)) - {"\t", "\n", "\r"}


class InvalidFilenameError(ValueError):
    """Raised when a filename is unsafe or carries an extension the app cannot read."""


class InvalidContentError(ValueError):
    """Raised when file content cannot be stored in the overlay without corrupting it."""


class FileNotInSetError(KeyError):
    """Raised when the named file is not present in the set."""


class TableNameTakenError(ValueError):
    """Raised when a new file in a table-by-table set has another file's table name.

    The name a program looks a table up by is the file name less its extension,
    so ``geo.csv`` beside ``geo.json`` would be two tables under one name.

    Attributes:
        name: The file being added.
        existing: The file already carrying that table name.
    """

    def __init__(self, name: str, existing: str) -> None:
        """Name both files and the table name they share."""
        super().__init__(
            f"{name!r} would be looked up as table {table_name(name)!r}, which "
            f"{existing!r} already is; rename one of the two"
        )
        self.name = name
        self.existing = existing


@dataclass(frozen=True, slots=True)
class AppFile:
    """One file an app consumes."""

    name: str
    content: str
    language: str

    @property
    def size_bytes(self) -> int:
        """The content's size as UTF-8, which is what the overlay stores."""
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


def validate_content(content: str) -> None:
    """Reject content the overlay cannot carry without being corrupted."""
    found = sorted(_FORBIDDEN_CONTENT.intersection(content))
    if found:
        codes = ", ".join(f"\\x{ord(c):02x}" for c in found)
        raise InvalidContentError(
            f"content contains control characters YAML cannot carry ({codes}); "
            "remove them or upload the file as text"
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
    # Refused rather than filtered: both mutators write this list back over the
    # whole key, so silently dropping an entry deletes a hand-authored file.
    for entry in raw:
        if not isinstance(entry, dict) or _NAME_FIELD not in entry:
            raise ValueError(
                f"{file_set.values_path} holds an entry without a {_NAME_FIELD!r} key; "
                "repair the overlay before editing its files"
            )
    return list(raw)


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
    validate_content(content)
    entries = _entries(doc, file_set)
    _refuse_a_second_table_name(file_set, [str(e[_NAME_FIELD]) for e in entries], name)
    body = literal_block(content)
    changed = True
    for entry in entries:
        if entry[_NAME_FIELD] == name:
            if str(entry.get(_CONTENT_FIELD, "")) == str(body):
                changed = False
                break
            entry[_CONTENT_FIELD] = body
            set_path(doc, file_set.values_path, entries)
            break
    else:
        entries.append({_NAME_FIELD: name, _CONTENT_FIELD: body})
        set_path(doc, file_set.values_path, entries)
    return sync_table_entries(doc, file_set) or changed


def delete_file(doc: dict, file_set: ConsumedFileSet, name: str) -> bool:
    """Remove a file from the set. Mutates ``doc``. Returns whether it changed.

    An entry the author declared for the file is kept for its key columns, and
    warned about as the Compose render warns, since the app cannot load it now.
    """
    entries = _entries(doc, file_set)
    remaining = [e for e in entries if e[_NAME_FIELD] != name]
    if len(remaining) == len(entries):
        raise FileNotInSetError(name)
    set_path(doc, file_set.values_path, remaining)
    sync_table_entries(doc, file_set)
    _warn_on_orphaned_entries(doc, file_set, name)
    return True


def _refuse_a_second_table_name(file_set: ConsumedFileSet, carried: list[str], name: str) -> None:
    """Refuse a new file whose table name another file in the set already has."""
    if not file_set.entries_path or name in carried:
        return
    for existing in carried:
        if table_name(existing) == table_name(name):
            raise TableNameTakenError(name, existing)


def _warn_on_orphaned_entries(doc: dict, file_set: ConsumedFileSet, name: str) -> None:
    """Warn about each entry still naming a file just removed from the set."""
    if not file_set.entries_path or not file_set.mount_path:
        return
    declared = get_path(doc, file_set.entries_path, default=None)
    path = f"{file_set.mount_path}/{name}"
    for entry in declared if isinstance(declared, list) else []:
        if isinstance(entry, dict) and entry.get("path") == path:
            logger.warning(
                "a declared entry names a file this set no longer carries",
                file_set=file_set.name,
                entry=entry.get(_NAME_FIELD),
                missing_file=name,
                path=path,
            )


def table_name(filename: str) -> str:
    """The name a program looks a mounted table up by: the file name less its extension."""
    return filename.rsplit(".", 1)[0]


def derived_entry(file_set: ConsumedFileSet, filename: str) -> dict[str, str]:
    """The ``{name, path}`` entry naming one file of the set where it is mounted."""
    return {_NAME_FIELD: table_name(filename), "path": f"{file_set.mount_path}/{filename}"}


def sync_table_entries(doc: dict, file_set: ConsumedFileSet) -> bool:
    """Name every file of a table-by-table set in the app's config. Mutates ``doc``.

    An entry the config already names is left alone, because the author's entry
    carries key columns a derived one cannot. A derived entry whose file the set
    no longer carries is dropped, since the app would fail to load it. Returns
    whether the entry list changed. A set read as a directory has none, and one
    whose manifest names no ``mount_path`` leaves them to the chart that mounts it.
    """
    if not file_set.entries_path or not file_set.mount_path:
        return False
    declared = get_path(doc, file_set.entries_path, default=None)
    if declared is not None and not isinstance(declared, list):
        raise ValueError(
            f"{file_set.entries_path} holds {type(declared).__name__}, expected a list of "
            "{name, path} entries"
        )
    current = list(declared or [])
    carried = [str(entry[_NAME_FIELD]) for entry in _entries(doc, file_set)]
    kept = [entry for entry in current if not _dead_derived(entry, file_set, set(carried))]
    named = {entry.get(_NAME_FIELD) for entry in kept if isinstance(entry, dict)}
    added: list[dict[str, str]] = []
    for name in carried:
        if table_name(name) not in named:
            added.append(derived_entry(file_set, name))
            named.add(table_name(name))
    wanted = kept + added
    if wanted == current:
        return False
    set_path(doc, file_set.entries_path, wanted)
    return True


def _dead_derived(entry: object, file_set: ConsumedFileSet, carried: set[str]) -> bool:
    """Whether ``entry`` is a derived entry for a file the set no longer carries."""
    if not isinstance(entry, dict) or set(entry) != {_NAME_FIELD, "path"}:
        return False
    filename = str(entry["path"]).removeprefix(f"{file_set.mount_path}/")
    return entry == derived_entry(file_set, filename) and filename not in carried
