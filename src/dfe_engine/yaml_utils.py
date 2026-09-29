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

import math
import os
import tempfile
import threading
from collections.abc import Mapping
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML, YAMLError
from ruamel.yaml.scalarstring import LiteralScalarString
from scalo.logger import logger

from dfe_engine.yaml_health import FailureReason, write_health

# ruamel YAML instances carry mutable parser/emitter state and are NOT
# thread-safe: a single shared instance dumped/loaded from two threads at once
# corrupts that state and can emit a file with two documents (a ComposerError on
# the next read). FastAPI runs sync handlers in a worker-thread pool and
# background tasks (e.g. sigma propagate wait=0) run in their own threads, so
# concurrent YAML ops across threads are real. Give each thread its own
# instances via thread-local storage - no shared mutable state, no lock.
_local = threading.local()


# Never wrap a scalar. ruamel folds a scalar past 80 columns, and re-reading
# turns each fold back into a space, so the content changes. Block scalars avoid
# folding, but the safe loader discards scalar style: content written as a block
# returns plain, and the next write of that document emits it folded.
_NO_WRAP = 1 << 30


def _safe() -> YAML:
    inst = getattr(_local, "safe", None)
    if inst is None:
        inst = YAML(typ="safe")
        inst.default_flow_style = False
        inst.width = _NO_WRAP
        _local.safe = inst
    return inst


def _rt() -> YAML:
    inst = getattr(_local, "rt", None)
    if inst is None:
        inst = YAML()
        inst.default_flow_style = False
        inst.preserve_quotes = True
        inst.width = _NO_WRAP
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
    with open(path, encoding="utf-8") as f:
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


class YamlWriteError(YAMLError):
    """A write refused because its data did not dump, or its YAML does not read back as it.

    Attributes:
        reason: ``dump`` or ``verify`` (see :data:`dfe_engine.yaml_health.FailureReason`).
        target: The file or deploy-repo path that kept its old content, or None for a string.
    """

    def __init__(self, reason: FailureReason, target: str | None, error: str) -> None:
        super().__init__(f"refused to write {target or 'a YAML string'}: {reason} failed ({error})")
        self.reason = reason
        self.target = target


def same_data(written: Any, read: Any) -> bool:
    """Whether YAML that read back as *read* holds the data *written*.

    A tuple reads back as a list and NaN never equals itself, so both compare by content.
    The walk is iterative: the data may nest as deep as the writer itself managed.
    """
    pending = [(written, read)]
    while pending:
        left, right = pending.pop()
        if isinstance(left, Mapping):
            if not isinstance(right, Mapping) or left.keys() != right.keys():
                return False
            pending.extend((left[key], right[key]) for key in left)
        elif isinstance(left, list | tuple):
            if not isinstance(right, list) or len(left) != len(right):
                return False
            pending.extend(zip(left, right, strict=True))
        elif isinstance(left, float) and math.isnan(left):
            if not (isinstance(right, float) and math.isnan(right)):
                return False
        elif left != right:
            return False
    return True


def _refusal(reason: FailureReason, target: str | None, error: str) -> YamlWriteError:
    """Log, count and record a refused write, and return the error to raise."""
    logger.error(
        "YAML write refused; the old content is kept", target=target, reason=reason, error=error
    )
    write_health().refused(target, reason, error)
    return YamlWriteError(reason, target, error)


def _verified_text(data: Any, target: str | None) -> str:
    """*data* as YAML text that reads back as *data* and encodes as UTF-8.

    Raises:
        YamlWriteError: The dump raised, or its text does not read back as *data*.
    """
    stream = StringIO()
    try:
        _rt().dump(data, stream)
    except BaseException as exc:
        # A failed dump leaves ruamel's document open; every later dump on the thread writes nothing.
        _local.rt = None
        if not isinstance(exc, Exception):
            raise
        # The type only: an exception message can quote the data, and some of it is secret.
        raise _refusal("dump", target, type(exc).__name__) from exc
    text = stream.getvalue()
    try:
        text.encode("utf-8")
        read = yaml_load_string(text)
    except (YAMLError, UnicodeEncodeError, RecursionError) as exc:
        raise _refusal("verify", target, type(exc).__name__) from exc
    if not same_data(data, read):
        raise _refusal("verify", target, "does not read back as the data")
    return text


def yaml_dump(data: Any, dest: str | Path) -> None:
    """
    Dump data to a YAML file, ATOMICALLY, refusing YAML that does not read back.

    The text is read back and compared with *data* before anything touches the
    destination. It then goes to a temp file in the destination directory that
    os.replace()s the target, so a concurrent reader always sees either the complete
    old file or the complete new one - never a partial or doubled write.

    Args:
        data: Data to serialize
        dest: Path to the output file

    Raises:
        YamlWriteError: The data did not dump, or its YAML does not read back as it.
            The destination keeps its old content.
    """
    path = Path(dest)
    target = str(path)
    text = _verified_text(data, target)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
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
    write_health().written(target)


def yaml_dump_string(data: Any, *, target: str | None = None) -> str:
    """
    Dump data to a YAML string that reads back as the data.

    Args:
        data: Data to serialize
        target: Where the caller will store the text. A refusal leaves it degraded,
            and a clean dump clears it; None when the text is not stored.

    Returns:
        YAML string representation

    Raises:
        YamlWriteError: The data did not dump, or its YAML does not read back as it.
    """
    text = _verified_text(data, target)
    if target is not None:
        write_health().written(target)
    return text


def block_scalar_safe(text: str) -> bool:
    """Whether ``text`` round-trips through a ``|`` block scalar unchanged.

    A block scalar cannot represent a carriage return, cannot end a line in
    whitespace, and takes its indentation from the first non-empty line - so
    content whose first line is indented further than a later one silently
    terminates the block early and the remainder parses as sibling YAML.
    """
    if "\r" in text:
        return False
    lines = text.split("\n")
    if any(line != line.rstrip() for line in lines):
        return False
    first = next((line for line in lines if line.strip()), "")
    return not (first[:1].isspace())


def literal_block(text: str) -> LiteralScalarString | str:
    """Emit ``text`` as a ``|`` block scalar where that is safe, else unchanged.

    A block scalar keeps a file body readable in a diff instead of collapsing it
    into one quoted line of ``\\n`` escapes. Content the block form would corrupt
    is returned as a plain string so the emitter quotes it instead: correctness
    outranks the nicer diff, and forcing the block form on unsafe content lets a
    file body break out of its own scalar and forge sibling keys.

    Args:
        text: The content to emit.

    Returns:
        The content, tagged for block style only when that is lossless.
    """
    if not text or not block_scalar_safe(text):
        return text
    return LiteralScalarString(text if text.endswith("\n") else text + "\n")


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, mutating *base* in-place.

    - dicts: merge recursively
    - lists: the override's list replaces the base's wholesale, as Helm does
    - sets:  union
    - type mismatch or non-container: override wins

    A list is replaced rather than appended because whoever sets one expects
    exactly that list back, and a merge re-run over its own output stays idempotent.
    """
    for key, nxt in override.items():
        if key not in base:
            base[key] = nxt
            continue
        prev = base[key]
        if isinstance(prev, dict) and isinstance(nxt, dict):
            deep_merge(prev, nxt)
        elif isinstance(prev, list) and isinstance(nxt, list):
            base[key] = nxt
        elif isinstance(prev, set) and isinstance(nxt, set):
            prev |= nxt
        else:
            base[key] = nxt
    return base
