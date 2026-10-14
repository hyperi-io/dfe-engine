#  Project:      dfe-engine
#  File:         tests/unit/test_log_field_names.py
#  Purpose:      No field the engine logs is masked for its name alone
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every structured field the engine logs keeps its value through scalo's masking.

scalo masks a field whose name holds a credential word anywhere in it, so a field named
``secret_path`` or ``auth_path`` reaches the log as a mask however harmless its value.
Each logger call is read from the source, and every keyword it passes is judged by
scalo's own matcher.
"""

import ast
from collections.abc import Iterator
from pathlib import Path

from scalo.logger.filters import is_credential_name

import dfe_engine

_LEVELS = frozenset(
    {"trace", "debug", "info", "success", "warning", "error", "exception", "critical", "log"}
)

# Personal data the logging standard masks in every line, which an operator never reads from a log.
_MASKED_ON_PURPOSE = frozenset({"email"})


def _is_logger(owner: ast.expr) -> bool:
    """Whether a call's receiver is named as a logger: ``logger``, ``log``, ``self._log``."""
    name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
    return "log" in name.lower()


def _logged_fields() -> Iterator[tuple[str, str]]:
    """Every keyword passed to a logger call in the engine, with where it is passed."""
    root = Path(dfe_engine.__file__).parent
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in _LEVELS or not _is_logger(node.func.value):
                continue
            for keyword in node.keywords:
                if keyword.arg:
                    yield keyword.arg, f"{path.relative_to(root)}:{node.lineno}"


def test_the_scan_reads_the_engine_s_logger_calls():
    names = {name for name, _where in _logged_fields()}
    assert {"login_method", "store_path", "user_id"} <= names


def test_no_logged_field_is_masked_for_its_name():
    masked = sorted(
        f"{name} at {where}"
        for name, where in _logged_fields()
        if is_credential_name(name) and name not in _MASKED_ON_PURPOSE
    )
    assert masked == []
