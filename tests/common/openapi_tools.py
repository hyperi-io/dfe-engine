"""The generators under ``openapi-spec/``, which sit beside the spec rather than in the package."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

SPEC_DIR = Path(__file__).resolve().parents[2] / "openapi-spec"

_ROUTE_MAP = "dfe_engine_route_map"


def load_route_map() -> ModuleType:
    """``openapi-spec/route_map.py``, loaded by path once per process."""
    if _ROUTE_MAP not in sys.modules:
        spec = importlib.util.spec_from_file_location(_ROUTE_MAP, SPEC_DIR / "route_map.py")
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {SPEC_DIR / 'route_map.py'}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_ROUTE_MAP] = module
        spec.loader.exec_module(module)
    return sys.modules[_ROUTE_MAP]
