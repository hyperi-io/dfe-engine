"""
Root conftest.py for DFE Engine tests.

Test structure:
- tests/unit/       - Unit tests (no external dependencies)
- tests/integration/ - Integration tests (require ClickHouse)
- tests/e2e/        - End-to-end tests

Markers:
- @pytest.mark.unit         - Unit tests (default)
- @pytest.mark.integration  - Integration tests
- @pytest.mark.e2e          - End-to-end tests
- @pytest.mark.slow         - Slow running tests

Run examples:
    pytest tests/unit/                      # Run all unit tests
    pytest -m integration                   # Run integration tests only
    pytest -m "not integration"             # Skip integration tests
    pytest -n 4                             # Run with 4 parallel workers

Docker Integration:
    The test suite automatically manages Docker containers for ClickHouse and PostgreSQL.
    - Containers are started if not already running
    - Containers persist after tests complete (for dev reuse)
    - Tests use isolated databases with unique prefixes
    - If DFE_CLICKHOUSE_HOST or DFE_POSTGRES_HOST point to non-localhost, Docker is skipped
"""

import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from scalo.logger import logger

# Shared RBAC test fixtures (client_as + the canonical fixture identities).
# Registered at the root so `client_as` is available wherever the app/api_settings
# fixtures exist (the API test tree).
pytest_plugins = ["tests.support.rbac_fixtures"]

# --------------------------------------------------------------------------
# Test-session guard 1/2: never start a live OTLP metrics/trace/log exporter.
# There is no OTLP collector at localhost:4317 in CI (or most dev machines); a
# live PeriodicExportingMetricReader/OTLPMetricExporter would retry on every
# export and block on shutdown ("Can't shutdown multiple times"), hanging the
# run. scalo>=2.29.7 already defaults the endpoint off, but pin it hard here so
# a stray OTEL_EXPORTER_OTLP_ENDPOINT / metrics config can never turn it back on
# under test. setdefault so an explicit override still wins if a test needs one.
for _otel_key, _otel_val in (
    ("OTEL_SDK_DISABLED", "true"),
    ("OTEL_METRICS_EXPORTER", "none"),
    ("OTEL_TRACES_EXPORTER", "none"),
    ("OTEL_LOGS_EXPORTER", "none"),
):
    os.environ.setdefault(_otel_key, _otel_val)

# --------------------------------------------------------------------------
# Declare the dev posture explicitly. `DFE_ENV` defaults to "production", and
# under that posture `DFESettings` refuses `auth.enabled: False` -- it would hand
# an unauthenticated request the admin role. Most tests here construct settings
# with auth off and want exactly the dev semantics, so say so rather than relying
# on a default that also has to be safe for a real deployment.
#
# setdefault, so a test run that deliberately wants the production posture
# (test_settings.py's own posture cases construct DFESettings directly) is not
# overridden.
os.environ.setdefault("DFE_ENV", "test")
os.environ.setdefault("DFE_AUTH_ENABLED", "false")

# Load .env file if present (before any other imports that might use settings)
try:
    from dotenv import load_dotenv

    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=True)
        logger.info(f"Loaded environment from {env_path}")
except ImportError:
    pass  # python-dotenv not installed, rely on shell-exported vars

# Reset settings to pick up dotenv values
try:
    from dfe_engine.settings import reset_settings

    reset_settings()
except ImportError:
    pass

# --------------------------------------------------------------------------
# Test-session guard: CH connections must FAIL FAST, never block the run.
#
# A hermetic unit/api test has no ClickHouse. When an endpoint (or the app
# lifespan) opens a CH connection, scalo's ReconnectingResilience retries a
# refused connection for the FULL budget - 60s per op, up to 300s while a
# CH-Cloud wake is in flight (settings.clickhouse.resilience). A handful of
# such ops stalled the suite for 30-70+ min and timed out CI. The connection
# REFUSED itself is instant (~2ms); it is the 60s retry budget that hangs.
#
# Shrink the DEFAULT resilience budget to ~50ms for the test session, so a
# CH-down op still raises ServiceUnavailable (-> API 503, semantics preserved)
# but in milliseconds, not minutes. This reaches BOTH direct-constructed test
# settings (which bypass the env cascade) and load_settings(). The CH resilience
# UNIT tests pass their budgets EXPLICITLY, so this default change never touches
# them; integration tests hit a READY CH (first attempt succeeds, no retry), so
# the budget is irrelevant there too.
try:
    from dfe_engine.settings import ClickHouseResilienceSettings

    for _ch_field, _ch_val in (
        ("budget_seconds", 0.05),
        ("waking_budget_seconds", 0.05),
        ("wait_initial", 0.01),
        ("wait_max", 0.02),
    ):
        ClickHouseResilienceSettings.model_fields[_ch_field].default = _ch_val
    ClickHouseResilienceSettings.model_rebuild(force=True)
except Exception:  # never let a settings refactor break test collection
    pass

# --------------------------------------------------------------------------
# Test-session guard: HTTP calls must FAIL FAST, never wait the retry budget.
#
# scalo's HttpClient / AsyncHttpClient (used by the HyperDX client, OIDC IdP
# adapters, the gitops forge, storage + query backends, service health probes,
# the CH-Cloud + Redpanda-Cloud control planes, and the auto-CLI) retry with
# Stamina - 3 attempts with exponential backoff by default. Against an
# unreachable dependency that is up to ~90s of real sleeping per call. Stamina's
# own test mode disables the backoff AND caps attempts at 1, so a would-be
# outbound call fails in ~0s (single attempt) instead of waiting the budget.
# Unit tests already stub their outbound calls; this is the backstop so a test
# that forgets can never wait-forever on HTTP. scalo tests the retry engine
# itself, so nothing here needs the real backoff.
try:
    import stamina

    stamina.set_testing(True, attempts=1)
except ImportError:  # stamina ships with scalo[http]; skip if absent
    pass

# Test statistics tracking
test_stats = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "test_files": {}}

# Docker configuration
DOCKER_COMPOSE_FILE = Path(__file__).parent.parent / "docker-compose.yml"
CONTAINER_STARTUP_TIMEOUT = 60  # seconds


def _is_localhost(host: str) -> bool:
    """Check if host is localhost or 127.0.0.1."""
    return host in ("localhost", "127.0.0.1", "::1", "")


def _get_configured_hosts() -> tuple[str, str]:
    """Get configured ClickHouse and PostgreSQL hosts from environment."""
    ch_host = os.getenv("DFE_CLICKHOUSE_HOST", os.getenv("CLICKHOUSE_HOST", "localhost"))
    pg_host = os.getenv("DFE_POSTGRES_HOST", os.getenv("POSTGRES_HOST", "localhost"))
    return ch_host, pg_host


def _container_is_running(container_name: str) -> bool:
    """Check if a Docker container is running."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _container_is_healthy(container_name: str) -> bool:
    """Check if a Docker container is healthy."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Health.Status}}", container_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip() == "healthy"
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _start_docker_services(profile: str = "test") -> bool:
    """Start Docker services using docker-compose with specified profile."""
    if not DOCKER_COMPOSE_FILE.exists():
        logger.warning(f"docker-compose.yml not found at {DOCKER_COMPOSE_FILE}")
        return False

    try:
        logger.info(f"Starting Docker services with profile '{profile}'...")
        result = subprocess.run(
            ["docker", "compose", "-f", str(DOCKER_COMPOSE_FILE), "--profile", profile, "up", "-d"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=DOCKER_COMPOSE_FILE.parent,
        )
        if result.returncode != 0:
            logger.error(f"Failed to start Docker services: {result.stderr}")
            return False
        return True
    except subprocess.SubprocessError as e:
        logger.error(f"Error starting Docker services: {e}")
        return False


def _wait_for_container_healthy(
    container_name: str, timeout: int = CONTAINER_STARTUP_TIMEOUT
) -> bool:
    """Wait for a container to become healthy."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        if _container_is_healthy(container_name):
            return True
        time.sleep(1)
    return False


def _ensure_docker_services() -> dict[str, bool]:
    """
    Ensure Docker services are running for tests.

    Returns dict with status of each service:
    - 'clickhouse': True if ClickHouse is available
    - 'postgres': True if PostgreSQL is available
    - 'docker_started': True if we started containers (vs already running)
    """
    ch_host, pg_host = _get_configured_hosts()
    status = {"clickhouse": False, "postgres": False, "docker_started": False}

    # Skip Docker if external hosts are configured
    if not _is_localhost(ch_host) or not _is_localhost(pg_host):
        logger.info(f"Using external services: ClickHouse={ch_host}, PostgreSQL={pg_host}")
        status["clickhouse"] = not _is_localhost(ch_host)
        status["postgres"] = not _is_localhost(pg_host)
        return status

    # Check if containers are already running
    ch_running = _container_is_running("dfe-clickhouse")
    pg_running = _container_is_running("dfe-postgres")

    if ch_running and pg_running:
        logger.info("Docker containers already running")
        status["clickhouse"] = _container_is_healthy("dfe-clickhouse")
        status["postgres"] = _container_is_healthy("dfe-postgres")
        return status

    # Start containers if needed
    if _start_docker_services("test"):
        status["docker_started"] = True

        # Wait for containers to be healthy
        if _wait_for_container_healthy("dfe-clickhouse"):
            status["clickhouse"] = True
            logger.info("ClickHouse container is healthy")
        else:
            logger.warning("ClickHouse container did not become healthy in time")

        if _wait_for_container_healthy("dfe-postgres"):
            status["postgres"] = True
            logger.info("PostgreSQL container is healthy")
        else:
            logger.warning("PostgreSQL container did not become healthy in time")

    return status


def pytest_collection_modifyitems(config, items):
    """Auto-mark tests based on their location."""
    for item in items:
        # Get the test file path relative to tests/
        test_path = Path(item.fspath)

        if "integration" in test_path.parts:
            item.add_marker(pytest.mark.integration)
        elif "e2e" in test_path.parts:
            item.add_marker(pytest.mark.e2e)
        else:
            # Default to unit tests
            item.add_marker(pytest.mark.unit)


def pytest_runtest_logreport(report):
    """Collect test statistics."""
    if report.when == "call":
        test_stats["total"] += 1

        test_file = report.nodeid.split("::")[0]
        if test_file not in test_stats["test_files"]:
            test_stats["test_files"][test_file] = {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
            }

        if report.passed:
            test_stats["passed"] += 1
            test_stats["test_files"][test_file]["passed"] += 1
        elif report.failed:
            test_stats["failed"] += 1
            test_stats["test_files"][test_file]["failed"] += 1
        elif report.skipped:
            test_stats["skipped"] += 1
            test_stats["test_files"][test_file]["skipped"] += 1

        test_stats["test_files"][test_file]["total"] += 1


@pytest.fixture(autouse=True)
def _no_clickhouse_bootstrap_hang(request):
    """Test-session guard 2/2: stop the app lifespan blocking on ClickHouse.

    ``create_app()``'s lifespan runs ``bootstrap_clickhouse()``, which opens a
    REAL ClickHouse connection (default ``localhost:9000``) and, when CH is
    unreachable, retries for the full resilience budget (60s, up to 300s while a
    CH-Cloud wake is in flight) on EACH bootstrap DDL. A hermetic unit/api test
    that spins a ``TestClient`` blocks in ``__enter__`` (lifespan startup) for
    that whole budget - a handful of such tests stalled the suite for 30-70+ min
    and timed out CI (arc-runner, no collector, no CH at :9000).

    The api conftest already disables it via
    ``ClickHouseSettings(bootstrap_tables=False)``; this is the session-wide
    backstop for any app fixture that forgets (test_alerts, test_services,
    test_deployments, test_fieldmaps, ... build their own settings). Integration
    / e2e / live tests keep the REAL bootstrap - they run against a real CH
    fixture and must exercise the true startup path.
    """
    if any(request.node.get_closest_marker(m) for m in ("integration", "e2e", "live")):
        yield
        return

    import dfe_engine.clickhouse.bootstrap as _chb

    original = _chb.bootstrap_clickhouse
    # lifespan does `from ...bootstrap import bootstrap_clickhouse` at call time,
    # so patching the module attribute is picked up when the app starts up.
    _chb.bootstrap_clickhouse = lambda *a, **k: None
    try:
        yield
    finally:
        _chb.bootstrap_clickhouse = original


@pytest.fixture(autouse=True)
def _no_kafka_broker_calls(request):
    """Test-session guard 3/3: no unit test reaches a real Kafka broker.

    A source deploy creates that source's topics, and ``kafka.ensure_topics``
    defaults on with ``bootstrap_servers`` defaulting to ``localhost:9092``. Any
    test that deploys a source therefore spends the full 10s admin timeout on a
    box with no broker - and on a box that HAS one (a developer's local Redpanda)
    it creates that test's topics on it for real. Both are wrong for a unit test.

    Patching the AdminClient constructor, rather than the setting, keeps the
    ensure path itself under test: it still runs, and reports the broker as
    unreachable. A test wanting the real adapter patches this name itself, and a
    test wanting a specific outcome injects its own ``admin=``.
    """
    if any(request.node.get_closest_marker(m) for m in ("integration", "e2e", "live")):
        yield
        return

    import dfe_engine.kafka.topics as _kt

    def _refuse(*_args, **_kwargs):
        raise RuntimeError("unit tests do not talk to a Kafka broker (conftest guard)")

    original = _kt.AdminClient
    _kt.AdminClient = _refuse
    try:
        yield
    finally:
        _kt.AdminClient = original


@pytest.fixture(scope="session")
def resources_path():
    """Path to test resources directory."""
    return Path(__file__).parent / "resources"


@pytest.fixture(scope="session")
def post_build_artefacts_path(resources_path):
    """Path to post_build_artefacts test data."""
    return resources_path / "post_build_artefacts"


# =============================================================================
# Docker Service Fixtures
# =============================================================================


@pytest.fixture(scope="session")
def docker_services():
    """
    Session-scoped fixture that ensures Docker services are running.

    This fixture:
    - Starts Docker containers if not already running
    - Waits for containers to be healthy
    - Returns status dict with service availability
    - Does NOT stop containers after tests (for dev reuse)

    Usage:
        def test_something(docker_services):
            if not docker_services['clickhouse']:
                pytest.skip("ClickHouse not available")
    """
    return _ensure_docker_services()


@pytest.fixture(scope="session")
def test_run_id():
    """Generate a unique ID for this test run (for database isolation)."""
    return uuid.uuid4().hex[:8]


@pytest.fixture(scope="session")
def clickhouse_test_database(docker_services, test_run_id):
    """
    Create an isolated ClickHouse database for this test run.

    The database is named: dfe_test_{run_id}
    It is cleaned up after the test session completes.
    """
    if not docker_services.get("clickhouse"):
        pytest.skip("ClickHouse not available")

    db_name = f"dfe_test_{test_run_id}"

    # Get connection settings
    host = os.getenv("DFE_CLICKHOUSE_HOST", "localhost")
    port = int(os.getenv("DFE_CLICKHOUSE_PORT", "8123"))
    user = os.getenv("DFE_CLICKHOUSE_USERNAME", os.getenv("DFE_CLICKHOUSE_USER", "default"))
    password = os.getenv("DFE_CLICKHOUSE_PASSWORD", "")

    try:
        import clickhouse_connect

        client = clickhouse_connect.get_client(
            host=host,
            port=port,
            username=user,
            password=password,
        )

        # Create test database
        client.command(f"CREATE DATABASE IF NOT EXISTS {db_name}")
        logger.info(f"Created test database: {db_name}")

        yield db_name

        # Cleanup: drop test database
        client.command(f"DROP DATABASE IF EXISTS {db_name}")
        logger.info(f"Dropped test database: {db_name}")

    except ImportError:
        pytest.skip("clickhouse-connect not installed")
    except Exception as e:
        logger.error(f"Failed to create test database: {e}")
        pytest.skip(f"ClickHouse connection failed: {e}")


@pytest.fixture
def clickhouse_client(clickhouse_test_database):
    """
    Provide a ClickHouse client connected to the test database.

    This is a function-scoped fixture that provides a fresh client
    for each test, connected to the isolated test database.
    """
    host = os.getenv("DFE_CLICKHOUSE_HOST", "localhost")
    port = int(os.getenv("DFE_CLICKHOUSE_PORT", "8123"))
    user = os.getenv("DFE_CLICKHOUSE_USERNAME", os.getenv("DFE_CLICKHOUSE_USER", "default"))
    password = os.getenv("DFE_CLICKHOUSE_PASSWORD", "")

    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=host,
        port=port,
        username=user,
        password=password,
        database=clickhouse_test_database,
    )
    return client
