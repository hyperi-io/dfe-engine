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

from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import (
    YAML,
    YAMLError,  # noqa: F401 - re-exported
)

# Create a safe YAML instance for loading untrusted content
_yaml_safe = YAML(typ="safe")
_yaml_safe.default_flow_style = False

# Create a round-trip YAML instance for preserving formatting
_yaml_rt = YAML()
_yaml_rt.default_flow_style = False
_yaml_rt.preserve_quotes = True
_yaml_rt.indent(mapping=2, sequence=4, offset=2)


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
        return _yaml_safe.load(f)


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
    return _yaml_safe.load(StringIO(content))


def yaml_dump(data: Any, dest: str | Path) -> None:
    """
    Dump data to a YAML file.

    Args:
        data: Data to serialize
        dest: Path to the output file
    """
    path = Path(dest)
    with open(path, "w") as f:
        _yaml_rt.dump(data, f)


def yaml_dump_string(data: Any) -> str:
    """
    Dump data to a YAML string.

    Args:
        data: Data to serialize

    Returns:
        YAML string representation
    """
    stream = StringIO()
    _yaml_rt.dump(data, stream)
    return stream.getvalue()


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, mutating *base* in-place.

    - dicts: merge recursively
    - lists: append override items
    - sets:  union
    - type mismatch or non-container: override wins
    """
    for key, nxt in override.items():
        if key not in base:
            base[key] = nxt
            continue
        prev = base[key]
        if isinstance(prev, dict) and isinstance(nxt, dict):
            deep_merge(prev, nxt)
        elif isinstance(prev, list) and isinstance(nxt, list):
            prev.extend(nxt)
        elif isinstance(prev, set) and isinstance(nxt, set):
            prev |= nxt
        else:
            base[key] = nxt
    return base
