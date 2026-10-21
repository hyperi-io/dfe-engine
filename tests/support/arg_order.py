#  Project:      dfe-engine
#  File:         tests/support/arg_order.py
#  Purpose:      Collect the command-line paths in path order
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Collect the command-line paths in path order, so each directory is entered once.

pytest 9.1.1 builds a second collector for a directory when a file argument
re-enters it after a file argument from its parent, and the second one has none
of the directory's conftest fixtures (pytest-dev/pytest#14635).
"""

from pathlib import Path

import pytest


def _path_parts(arg: str, base: Path) -> tuple[str, ...]:
    return (base / arg.split("::", 1)[0]).resolve().parts


@pytest.hookimpl(tryfirst=True)
def pytest_collection(session: pytest.Session) -> None:
    """Sort the collection arguments by path before the session collects them."""
    # SHORTCUT: needed only while the pinned pytest predates the #14635 fix, delete once a release carries it.
    base = session.config.invocation_params.dir
    session.config.args[:] = sorted(session.config.args, key=lambda arg: _path_parts(arg, base))
