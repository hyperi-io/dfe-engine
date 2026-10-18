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
"""

import os
import tempfile
from pathlib import Path

import pytest
from dulwich.errors import NotGitRepository
from dulwich.repo import Repo
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

# Never post to the release endpoint: create_app() wires scalo's startup version
# check with a real api_url, and version_check.enabled is read from the
# bare-prefix cascade (Dynaconf, so VERSION_CHECK__ENABLED). Off for the run.
os.environ.setdefault("VERSION_CHECK__ENABLED", "false")

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

# Load .env file if present (before any other imports that might use settings).
# Only this tree's own: a worktree never reads the main checkout's, and no parent
# directory's .env is loaded into the test process.
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

# Same guard for a deploy-repo write: a test whose remote is unreachable fails in
# milliseconds instead of retrying for the production budget. Tests of the retry
# itself pass their own GitopsWriteSettings.
try:
    from dfe_engine.settings import GitopsWriteSettings

    for _gw_field, _gw_val in (
        ("budget_seconds", 0.05),
        ("wait_initial", 0.01),
        ("wait_max", 0.02),
    ):
        GitopsWriteSettings.model_fields[_gw_field].default = _gw_val
    GitopsWriteSettings.model_rebuild(force=True)
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


def _enclosing_repository(path: Path) -> Path | None:
    """Return the repository a DirectoryConfigStore under ``path`` would commit to."""
    try:
        with Repo.discover(str(path.resolve())) as repo:
            return Path(repo.path)
    except NotGitRepository:
        return None


def pytest_configure(config: pytest.Config) -> None:
    """Cap the HTTP client loggers, and refuse a run whose temp roots sit inside a git repository.

    ``log_cli_level`` prints INFO, so without the engine's cap every request URL a
    test sends, a user's login in a directory lookup included, lands in the run's log.

    scalo's DirectoryConfigStore walks up to the nearest repository, so every
    registry write under such a root commits onto that repository's branch.
    """
    from dfe_engine.api import cap_http_client_loggers

    cap_http_client_loggers()
    basetemp = config.option.basetemp
    pytest_root = os.environ.get("PYTEST_DEBUG_TEMPROOT") or tempfile.gettempdir()
    roots = [
        ("--basetemp", basetemp) if basetemp else ("the pytest temp root", pytest_root),
        ("TMPDIR", tempfile.gettempdir()),
    ]
    for label, root in roots:
        repository = _enclosing_repository(Path(root))
        if repository is not None:
            raise pytest.UsageError(
                f"{label} {root} is inside the git repository at {repository}, so every "
                "registry write under it would commit onto that repository's branch. "
                "Point --basetemp and TMPDIR outside any git checkout."
            )


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


# Markers whose tests talk to real ClickHouse / Kafka. In-process tests under
# tests/e2e/ are auto-marked ``e2e`` but use TestClient and must NOT skip the
# hang guards - a local CH/Redpanda otherwise wedges lifespan startup.
REAL_INFRA_MARKERS = ("integration", "live")


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
    / live tests keep the REAL bootstrap - they run against a real CH fixture
    and must exercise the true startup path.
    """
    if any(request.node.get_closest_marker(m) for m in REAL_INFRA_MARKERS):
        yield
        return

    import dfe_engine.clickhouse.bootstrap as _chb
    from dfe_engine.schema.phase import SchemaBootstrapState

    original = _chb.bootstrap_clickhouse
    # lifespan does `from ...bootstrap import bootstrap_clickhouse` at call time,
    # so patching the module attribute is picked up when the app starts up. The
    # stand-in returns the phase's own state shape, unknown, which is what a
    # deployment with the bootstrap switched off reports.
    _chb.bootstrap_clickhouse = lambda *a, **k: SchemaBootstrapState()
    try:
        yield
    finally:
        _chb.bootstrap_clickhouse = original


@pytest.fixture(autouse=True)
def _no_kafka_broker_calls(request):
    """Test-session guard 3/3: no unit test reaches a real Kafka broker.

    A source deploy creates that source's topics, and the default deployment has
    a bus, with ``bootstrap_servers`` defaulting to ``localhost:9092``. Any
    test that deploys a source therefore spends the full 10s admin timeout on a
    box with no broker - and on a box that HAS one (a developer's local Redpanda)
    it creates that test's topics on it for real. Both are wrong for a unit test.

    Patching the AdminClient constructor, rather than the setting, keeps the
    ensure path itself under test: it still runs, and reports the broker as
    unreachable. A test wanting the real adapter patches this name itself, and a
    test wanting a specific outcome injects its own ``admin=``.
    """
    if any(request.node.get_closest_marker(m) for m in REAL_INFRA_MARKERS):
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


@pytest.fixture(autouse=True)
def _no_ch_rbac_reconcile_against_a_real_clickhouse(request):
    """Test-session guard: no unit test reconciles CH RBAC against a real ClickHouse.

    The app reconciles at startup and after every org or group write, against
    ``settings.clickhouse`` -- ``localhost:8123`` by default, which is where a
    developer's local stack publishes its ClickHouse. The reconcile drops users and
    rewrites row policies, so a unit run would rewrite that stack.

    Refusing the admin client rather than switching the reconcile off keeps the
    path under test: startup and the change trigger still run it, and see it fail
    as they would a ClickHouse that is down. The lifespan and the governance
    endpoint import the name at call time, so the package attribute is what they
    get.
    """
    if any(request.node.get_closest_marker(m) for m in REAL_INFRA_MARKERS):
        yield
        return

    import dfe_engine.governance.ch as _ch_rbac
    import dfe_engine.governance.ch.bootstrap as _ch_rbac_bootstrap

    def _refuse(*_args, **_kwargs):
        raise RuntimeError("unit tests do not reconcile against a ClickHouse (conftest guard)")

    originals = (_ch_rbac.ch_admin_client, _ch_rbac_bootstrap.ch_admin_client)
    _ch_rbac.ch_admin_client = _refuse
    _ch_rbac_bootstrap.ch_admin_client = _refuse
    try:
        yield
    finally:
        _ch_rbac.ch_admin_client, _ch_rbac_bootstrap.ch_admin_client = originals
