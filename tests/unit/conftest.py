"""Shared fixtures for tests/unit/.

Test-session guard: every bcrypt hash in this tree runs at the minimum cost.
"""

import threading
from collections.abc import Iterator

import bcrypt
import pytest
from scalo.logger import logger

from dfe_engine.governance.ch.trigger import WORKER_THREAD_NAME
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


@pytest.fixture(autouse=True)
def _no_reconcile_thread_outlives_its_test():
    """Fail a test that leaves a CH RBAC reconcile thread running after it.

    A trigger nobody closes retries a failed run on a back-off of up to five minutes,
    and logs into whatever sink is left once pytest has closed its capture. The app
    closes the triggers on its ``app.state`` at shutdown, so a leak is a test that
    replaced one without closing it. Only threads this test started are counted, so
    one leak fails one test.
    """
    before = set(threading.enumerate())
    yield
    started = [t for t in threading.enumerate() if t.name == WORKER_THREAD_NAME and t not in before]
    for thread in started:
        # A closed trigger's worker is already leaving.
        thread.join(timeout=1.0)
    leaked = [t for t in started if t.is_alive()]
    assert not leaked, (
        f"{len(leaked)} {WORKER_THREAD_NAME} thread(s) still running: close the trigger"
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


@pytest.fixture
def audit_events() -> Iterator[list[dict]]:
    """Every event the engine logs while the test runs, from a real sink on its logger.

    Each entry is the event name under ``event`` plus the structured fields it carried.
    The sink takes every level, so a test that asserts a value is absent reads the
    debug lines too.
    """
    events: list[dict] = []

    def record(message) -> None:
        events.append({"event": message.record["message"], **message.record["extra"]})

    handler = logger.add(record, level="DEBUG", format="{message}")
    try:
        yield events
    finally:
        logger.remove(handler)
