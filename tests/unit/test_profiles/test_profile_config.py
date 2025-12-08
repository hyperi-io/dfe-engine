import pytest

# Skip all tests in this module - CLI tests belong to dfe-cli, not dfe-engine library
pytestmark = pytest.mark.skip(reason="CLI tests belong to dfe-cli package, not dfe-engine library")
