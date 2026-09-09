#  Project:      dfe-engine
#  File:         gitops/pins.py
#  Purpose:      The deploy repo's pins.yaml - the one reader
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""pins.yaml: what version of DFE this environment is pinned to.

``base`` selects ONE certified stack; ``overrides`` moves individual components
off that set at the operator's own risk (dfe-docs/deployment/stack-versioning.md).
Two surfaces read it - the SUPPORT-DRIFT startup notice and the console's version
footer - so the load, the shape checks and the never-raise policy live here and
nowhere else. Callers load once and pass the mapping to the accessors.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAMLError
from scalo.logger import logger

from dfe_engine.yaml_utils import yaml_load

PINS_FILE = "pins.yaml"

# One pin selects the whole certified stack, so dfe-infra's version IS the stack's.
STACK_COMPONENT = "dfe-infra"
UI_COMPONENT = "dfe-ui"


def load_pins(deploy_repo_path: str | Path) -> dict:
    """The deploy repo's pins.yaml as a mapping.

    Empty when the file is absent, unreadable or not a mapping: every caller is a
    read-only surface with a safe answer without it, so this never raises.
    """
    path = Path(deploy_repo_path) / PINS_FILE
    if not path.is_file():
        return {}
    try:
        data = yaml_load(path)
    except (OSError, YAMLError):  # malformed pins.yaml is the operator's problem, not a crash
        logger.warning(f"could not parse {path}; treating the environment as unpinned")
        return {}
    return data if isinstance(data, dict) else {}


def stack_version(pins: dict) -> str | None:
    """The certified stack this environment is pinned to; None when unpinned."""
    base = pins.get("base")
    if not isinstance(base, dict):
        return None
    pinned = base.get(STACK_COMPONENT)
    return str(pinned) if pinned else None


def component_overrides(pins: dict) -> dict[str, str]:
    """Every ``overrides`` group flattened to ``name -> ref``."""
    overrides = pins.get("overrides")
    if not isinstance(overrides, dict):
        return {}
    flat: dict[str, str] = {}
    for group in overrides.values():
        if isinstance(group, dict):
            for name, ref in group.items():
                flat[str(name)] = str(ref)
    return flat
