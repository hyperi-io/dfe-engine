#  Project:      dfe-engine
#  File:         tests/support/present.py
#  Purpose:      Narrow an optional value a test expects to be set
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""An optional value a test has set up, read as the value itself."""


def present[T](value: T | None) -> T:
    """Return *value*, failing the test where it is None.

    Args:
        value: An optional the test arranged to hold something.

    Returns:
        The value, narrowed from its optional type.
    """
    assert value is not None, "expected a value, got None"
    return value
