#  Project:      dfe-engine
#  File:         yaml_utils.py
#  Purpose:      Consolidated YAML operations using ruamel.yaml
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2025 HYPERI PTY LIMITED

"""
YAML utilities using ruamel.yaml.

This module provides a consistent interface for YAML operations across dfe-engine,
using ruamel.yaml which supports YAML 1.2 and preserves comments/formatting.

Usage:
    from dfe_engine.yaml_utils import yaml_load, yaml_dump, YAMLError

    # Load YAML
    data = yaml_load(file_path)
    data = yaml_load_string(yaml_string)

    # Dump YAML
    yaml_dump(data, file_path)
    yaml_string = yaml_dump_string(data)
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
