#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_core_dirs.py
#  Purpose:      Every core manifest definition sits under a directory an overlay may not redefine
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""An overlay object defining a file under a core directory is refused by name.

A core directory missing from ``CORE_DIRS`` lets an overlay shadow a core
definition's file, so every directory the pinned manifest defines from is covered.
"""

from dfe_engine.schema.plan import CORE_DIRS, _manifest, _redefines_core, core_schemas_root


def test_every_core_definition_sits_under_a_core_directory():
    manifest = _manifest(core_schemas_root())

    uncovered = sorted(
        str(obj.defines) for obj in manifest.objects if obj.defines and not _redefines_core(obj)
    )

    assert uncovered == [], f"add their directories to CORE_DIRS {CORE_DIRS}"
