#  Project:      dfe-engine
#  File:         yaml_utils.py
#  Purpose:      Consolidated YAML operations using ruamel.yaml
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2025 HYPERI PTY LIMITED

"""
YAML and data utilities for dfe-engine.

Provides YAML operations via ruamel.yaml (YAML 1.2, preserves comments)
and a recursive deep-merge for dict/list/set composition.

Usage:
    from dfe_engine.yaml_utils import yaml_load, yaml_dump, deep_merge

    data = yaml_load(file_path)
    deep_merge(base, overrides)  # mutates base in-place
"""

import os
import tempfile
import threading
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import (
    YAML,
    YAMLError,  # noqa: F401 - re-exported
)
from ruamel.yaml.scalarstring import LiteralScalarString

# ruamel YAML instances carry mutable parser/emitter state and are NOT
# thread-safe: a single shared instance dumped/loaded from two threads at once
# corrupts that state and can emit a file with two documents (a ComposerError on
# the next read). FastAPI runs sync handlers in a worker-thread pool and
# background tasks (e.g. sigma propagate wait=0) run in their own threads, so
# concurrent YAML ops across threads are real. Give each thread its own
# instances via thread-local storage - no shared mutable state, no lock.
_local = threading.local()


def _safe() -> YAML:
    inst = getattr(_local, "safe", None)
    if inst is None:
        inst = YAML(typ="safe")
        inst.default_flow_style = False
        _local.safe = inst
    return inst


def _rt() -> YAML:
    inst = getattr(_local, "rt", None)
    if inst is None:
        inst = YAML()
        inst.default_flow_style = False
        inst.preserve_quotes = True
        inst.indent(mapping=2, sequence=4, offset=2)
        _local.rt = inst
    return inst


def yaml_load(source: str | Path) -> Any:
    """
    Load YAML from a file path.

    Args:
        source: Path to the YAML file

    Returns:
        Parsed YAML content

    Raises:
        YAMLError: If the YAML is invalid
        FileNotFoundError: If the file doesn't exist
    """
    path = Path(source)
    with open(path) as f:
        return _safe().load(f)


def yaml_load_string(content: str) -> Any:
    """
    Load YAML from a string.

    Args:
        content: YAML string content

    Returns:
        Parsed YAML content

    Raises:
        YAMLError: If the YAML is invalid
    """
    return _safe().load(StringIO(content))


def yaml_dump(data: Any, dest: str | Path) -> None:
    """
    Dump data to a YAML file, ATOMICALLY.

    Writes to a temp file in the destination directory and os.replace()s it
    over the target, so a concurrent reader always sees either the complete
    old file or the complete new one - never a partial or doubled write.

    Args:
        data: Data to serialize
        dest: Path to the output file
    """
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            _rt().dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # Never leave a stray temp file behind on failure.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def yaml_dump_string(data: Any) -> str:
    """
    Dump data to a YAML string.

    Args:
        data: Data to serialize

    Returns:
        YAML string representation
    """
    stream = StringIO()
    _rt().dump(data, stream)
    return stream.getvalue()


def literal_block(text: str) -> LiteralScalarString:
    """Mark a string to be emitted as a ``|`` block scalar.

    Without this a multi-line string dumps as one quoted line full of ``\\n``
    escapes, which is unreadable in a diff and unusable as a file body carried
    inside a values document. A block scalar needs a trailing newline to round-trip,
    so one is added when absent.

    Args:
        text: The content to emit verbatim.

    Returns:
        The same content, tagged for literal block style.
    """
    return LiteralScalarString(text if text.endswith("\n") else text + "\n")


def deep_merge(base: dict, override: dict, *, replace_lists: bool = False) -> dict:
    """Recursively merge *override* into *base*, mutating *base* in-place.

    - dicts: merge recursively
    - lists: append override items, OR replace wholesale when ``replace_lists``
    - sets:  union
    - type mismatch or non-container: override wins

    ``replace_lists=True`` gives override-wins list semantics (matching Helm's own
    list behaviour). Required by any caller that re-merges its OWN prior output --
    e.g. the gitops publish merge and the sigma local-edit merge -- where the
    default APPEND duplicates every shared list on every pass, growing unboundedly
    and making the operation non-idempotent.
    """
    for key, nxt in override.items():
        if key not in base:
            base[key] = nxt
            continue
        prev = base[key]
        if isinstance(prev, dict) and isinstance(nxt, dict):
            deep_merge(prev, nxt, replace_lists=replace_lists)
        elif isinstance(prev, list) and isinstance(nxt, list):
            if replace_lists:
                base[key] = nxt
            else:
                prev.extend(nxt)
        elif isinstance(prev, set) and isinstance(nxt, set):
            prev |= nxt
        else:
            base[key] = nxt
    return base
