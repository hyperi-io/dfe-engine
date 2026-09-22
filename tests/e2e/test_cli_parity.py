#  Project:      dfe-engine
#  File:         tests/e2e/test_cli_parity.py
#  Purpose:      The CLI reaches the same engine facts the API does
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The other window onto the engine, checked against the first.

The UI and ``dfe`` are two windows onto one engine. Every other suite here drives
the API, so a capability reachable from the API and broken from the CLI is a
defect no test catches. This one asks the two for the same fact and compares the
answers.

That framing is the point: it is a PARITY suite, not a second end-to-end. It does
not re-prove that a source deploys -- ``test_filebeat_pipeline`` and the flow
suite already do that. It proves the CLI SEES what the API sees, which is the one
thing those suites cannot tell you.

The command tree is generated from ``create_app().openapi()`` at runtime
(``cli/auto/spec.py``), so a route the engine serves becomes a command without
anyone wiring it. ``test_command_tree_covers_the_source_lifecycle`` guards that
generation and needs NO live deployment, so it runs everywhere. The parity cases
need a real engine and skip without one.

Runs the installed ``dfe`` console script rather than importing the module: what
ships is a binary on a PATH, and importing it would prove the library works while
saying nothing about the entry point. One login serves both windows -- the token
``EngineAPI`` mints is handed to the CLI through its env, so the two are
demonstrably talking to the same engine as the same user. ``DFE_CONFIG_HOME`` is
redirected at a tmp dir so a run never reads or writes a developer's profile.

Skipped unless the DFE_E2E_* env is set - see conftest and README-live.md.
"""

import json
import os
import shutil
import subprocess

import pytest

from tests.e2e.conftest import E2EConfig, must, require
from tests.e2e.engine_api import EngineAPI

pytestmark = pytest.mark.live

# Every command the four-part proof drives. Generated from the OpenAPI spec, so
# one going missing means a route was withdrawn or the generator stopped reaching
# it -- either way the CLI silently loses the operation.
SOURCE_LIFECYCLE = ("create", "deploy", "plan", "describe", "list", "delete", "reconcile-apps")

# Long enough for a deploy against a real ClickHouse, short enough that a hung
# broker fails the test rather than the session.
CLI_TIMEOUT_S = 120


@pytest.fixture(scope="module")
def engine(e2e: E2EConfig) -> EngineAPI:
    """The engine API, or the skip that says which var is missing."""
    require(e2e, "engine_url", "engine_password")
    api = EngineAPI(
        base=must(e2e.engine_url).rstrip("/"),
        user=e2e.engine_user,
        password=must(e2e.engine_password),
        verify=e2e.verify,
    )
    api.login()
    return api


def _cli_path() -> str:
    """The installed console script, or the skip that says it is absent."""
    found = shutil.which("dfe")
    if found is None:
        pytest.skip("the 'dfe' console script is not on PATH - install the package to run this")
    return found


def _run(
    *args: str,
    url: str | None = None,
    token: str | None = None,
    expect_ok: bool = True,
) -> subprocess.CompletedProcess:
    """Invoke the CLI against its own config root, never the developer's.

    ``url`` and ``token`` are unset for the commands that need no engine
    (``--help``), which is what lets the command-tree test run with no
    deployment at all.
    """
    env = dict(os.environ)
    env["DFE_CONFIG_HOME"] = os.path.join(os.environ.get("TMPDIR", "/tmp"), "dfe-cli-parity")
    if url is not None:
        env["DFE_API_URL"] = url
    if token is not None:
        env["DFE_API_TOKEN"] = token

    proc = subprocess.run(
        [_cli_path(), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CLI_TIMEOUT_S,
        env=env,
        check=False,
    )
    if expect_ok and proc.returncode != 0:
        pytest.fail(
            f"dfe {' '.join(args)} exited {proc.returncode}\n"
            f"stdout: {proc.stdout[:2000]}\nstderr: {proc.stderr[:2000]}"
        )
    return proc


def _json(engine: EngineAPI, *args: str):
    """Run a command in JSON mode and parse it.

    ``--format json`` explicitly: the default is tty-dependent, and a test that
    parsed whatever a tty happened to produce would assert the formatter rather
    than the data.
    """
    proc = _run("--format", "json", *args, url=engine.base, token=engine.token)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"dfe {' '.join(args)} did not return JSON: {exc}\n{proc.stdout[:2000]}")


def _names(payload) -> set[str]:
    """Source names out of either shape the list endpoint may return."""
    rows = payload.get("items", payload) if isinstance(payload, dict) else payload
    return {row["source"] for row in rows if isinstance(row, dict) and "source" in row}


def test_command_tree_covers_the_source_lifecycle():
    """The generated tree still reaches every source operation.

    No engine needed: the tree is built from the app, so this catches a
    generation regression on any machine. It guards the property the CLI is
    meant to have -- it follows the API rather than being hand-wired.
    """
    listed = _run("sources", "--help").stdout

    missing = [cmd for cmd in SOURCE_LIFECYCLE if cmd not in listed]

    assert not missing, (
        f"dfe sources is missing {missing} -- either the route went away or the "
        f"OpenAPI generator stopped reaching it. Tree:\n{listed}"
    )


class TestParityWithTheAPI:
    """The same question, asked twice, compared."""

    def test_cli_lists_the_sources_the_api_lists(self, engine: EngineAPI):
        """The core assertion: two windows, one answer.

        A CLI returning a SUBSET is the interesting failure -- it looks like it
        works right up until the source you wanted is the one it dropped.
        """
        from_cli = _names(_json(engine, "sources", "list"))
        from_api = _names(engine.json("GET", "/api/v1/sources"))

        assert from_cli == from_api, (
            "the CLI and the API disagree about which sources exist\n"
            f"  only in the CLI: {sorted(from_cli - from_api)}\n"
            f"  only in the API: {sorted(from_api - from_cli)}"
        )

    def test_cli_describes_a_source_the_api_describes_the_same_way(self, engine: EngineAPI):
        """Parity on the record, not only on the name list."""
        names = sorted(_names(engine.json("GET", "/api/v1/sources")))
        if not names:
            pytest.skip("no sources on this deployment to describe")
        name = names[0]

        from_cli = _json(engine, "sources", "describe", name)
        from_api = engine.json("GET", f"/api/v1/sources/{name}")

        # Compared on the fields an operator acts on. The whole document would
        # drag in server-set timestamps that differ between two reads and prove
        # nothing about the CLI.
        for field in ("source", "current", "deployed_version"):
            assert from_cli.get(field) == from_api.get(field), (
                f"{field} differs between the CLI and the API for {name!r}: "
                f"{from_cli.get(field)!r} vs {from_api.get(field)!r}"
            )
