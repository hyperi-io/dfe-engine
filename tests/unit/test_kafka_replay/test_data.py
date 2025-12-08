import pytest

# Skip - kafka_replay module not yet migrated to dfe-engine
pytestmark = pytest.mark.skip(reason="kafka_replay module not yet migrated")

FIXED_CURRENT_TIMESTAMP = None
TEST_PARSE_TIMESTAMP = []
TEST_CREATE_KEY_PREFIXES = []
TEST_MANIPULATE_LOG = []
