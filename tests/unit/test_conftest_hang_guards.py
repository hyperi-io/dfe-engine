#  Project:      dfe-engine
#  File:         tests/unit/test_conftest_hang_guards.py
#  Purpose:      In-process e2e TestClient tests must not open live CH/Kafka
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The hang guards in tests/conftest.py.

In-process tests under tests/e2e/ are auto-marked ``e2e``. They use TestClient
and claim no external infrastructure. If the CH/Kafka hang guards skip them,
a developer machine with localhost ClickHouse or Redpanda wedges the suite:
lifespan bootstrap talks to the real broker, pytest-timeout cannot interrupt
the C client, and ``make test`` sits at ~94% until the job is killed.
"""

from tests.conftest import REAL_INFRA_MARKERS


def test_in_process_e2e_still_gets_the_hang_guards():
    """Only integration and live tests may open real CH/Kafka."""
    assert "e2e" not in REAL_INFRA_MARKERS
    assert REAL_INFRA_MARKERS == ("integration", "live")
