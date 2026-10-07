"""Shared fixtures for tests/unit/.

Test-session guard: every bcrypt hash in this tree runs at the minimum cost.
"""

from collections.abc import Iterator
from pathlib import Path

import bcrypt
import pytest
from scalo.logger import logger

from dfe_engine.yaml_health import YamlWriteMetrics, write_health
from tests.unit.test_auth.factories import OKTA_API_TOKEN_ENV, make_local_directory
from tests.unit.test_auth.test_oidc.local_directory import LocalDirectory

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


@pytest.fixture
def http_directory() -> Iterator[LocalDirectory]:
    """Run a plain-HTTP directory API on 127.0.0.1 for the duration of one test."""
    directory = make_local_directory(tls_dir=None)
    directory.start()
    yield directory
    directory.stop()


@pytest.fixture
def log_lines() -> Iterator[list[str]]:
    """Collect each log line at WARNING or above, with its structured fields, for the duration of one test."""
    lines = []
    sink = logger.add(lines.append, format="{message} {extra}", level="WARNING")
    yield lines
    logger.remove(sink)


@pytest.fixture
def okta_api_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Put an Okta API token in the env var a provider from make_okta_directory_provider reads and return it."""
    token = "test-okta-api-token"
    monkeypatch.setenv(OKTA_API_TOKEN_ENV, token)
    return token


@pytest.fixture
def tls_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[LocalDirectory]:
    """Run a TLS directory API on 127.0.0.1 whose CA the engine's HTTP client trusts, for the duration of one test."""
    directory = make_local_directory(tls_dir=tmp_path / "directory-tls")
    monkeypatch.setenv("SSL_CERT_FILE", str(directory.ca_file))
    directory.start()
    yield directory
    directory.stop()
