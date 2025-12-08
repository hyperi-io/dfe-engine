import pytest

# Skip - requires line_profiler dev dependency
pytestmark = pytest.mark.skip(reason="requires line_profiler dev dependency")
