"""Shared fixtures for tests/unit/.

Test-session guard: every bcrypt hash in this tree runs at the minimum cost.
"""

import bcrypt
import pytest

from dfe_engine.yaml_health import YamlWriteMetrics, write_health

# bcrypt's own floor -- gensalt() accepts 4-31, so this is not an arbitrary
# choice, it is the cheapest hash bcrypt can produce.
_TEST_BCRYPT_ROUNDS = 4

# Captured before the fixture below patches the module attribute, so the
# real KDF still runs -- only the requested cost is overridden.
_real_gensalt = bcrypt.gensalt


@pytest.fixture(autouse=True)
def _fast_bcrypt(monkeypatch):
    """Hash every account password at bcrypt's minimum cost in this tree.

    Production hashes at cost 12 (~0.28s/hash on the CI host); the
    test_api ``client`` fixture alone triggers at least three hashes per
    test across 874+ tests, so at cost 12 that fixture alone accounts for
    minutes of the Test job. No test in this tree asserts the numeric
    cost -- only the ``$2b$`` format prefix -- so forcing cost 4 changes
    nothing a test checks. ``src/`` is untouched: production keeps cost 12.
    """
    monkeypatch.setattr(
        bcrypt,
        "gensalt",
        lambda rounds=12, prefix=b"2b": _real_gensalt(_TEST_BCRYPT_ROUNDS, prefix),
    )


def _forget_refused_writes() -> None:
    health = write_health()
    health.bind(YamlWriteMetrics())
    for entry in health.degraded():
        health.written(entry.target)


@pytest.fixture(autouse=True)
def _fresh_write_health():
    """Start and end every test with no refused YAML write recorded and no counter bound.

    The writer's health is per process, so a refusal one test provokes would otherwise
    show in a later test's status report.
    """
    _forget_refused_writes()
    yield
    _forget_refused_writes()
