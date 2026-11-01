#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_match_delivery.py
#  Purpose:      A hunt fire that matched reaches its alert destination
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A fire that wrote rows sends one message to each destination the hunt names.

The end-to-end case runs the real chain: hunt YAML on disk -> ``load_specs`` -> a
runner tick -> the worker -> the destination registry -> Apprise -> an HTTP
listener standing in for the webhook. Only ClickHouse is faked, because the
delivery depends on the rows a fire reports and not on where they went.
"""

import json
import threading
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from dfe_engine.hunt_runner import HuntRunner, HuntSpec, HuntWorker, load_specs
from dfe_engine.hunt_runner.delivery import MatchNotifier
from dfe_engine.hunts.alert import AlertDestination, AlertDestinationRegistry, AlertDispatcher

HUNT_YAML = """\
cron: "*/5 * * * *"
query: "INSERT INTO dfe.detection SELECT * FROM dfe.events WHERE {window}"
alerts:
  destinations:
    - ops-webhook
  triggers:
    - when: "result_count > 2"
"""


class _Webhook:
    """Records every POST body it receives."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []
        bodies = self.bodies

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                bodies.append(json.loads(self.rfile.read(length)))
                self.send_response(200)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"json://127.0.0.1:{self._server.server_port}/hook"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _Webhook:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class _Summary:
    def __init__(self, written_rows: int) -> None:
        self.written_rows = written_rows


class _Ch:
    def __init__(self, written_rows: int) -> None:
        self._written_rows = written_rows

    def command(self, sql: str, settings: dict | None = None) -> _Summary:
        return _Summary(self._written_rows)


class _Coordinator:
    """Just enough of ChCoordinator for one tick of one hunt."""

    def __init__(self) -> None:
        self.watermark: int | None = None

    def get_watermark(self, hunt_id: str) -> int | None:
        return self.watermark

    def set_watermark(self, hunt_id: str, watermark: int) -> None:
        self.watermark = watermark

    def record_run(self, hunt_id: str, fire: int, rows_written: int) -> None:
        pass

    def heartbeat(self, now: int, poll_seconds: float) -> None:
        pass

    def active_count(self, now: int) -> int:
        return 0

    def pending_runs(self, now: int) -> dict[str, int]:
        return {}

    def current_lease(self, hunt_id: str) -> None:
        return None

    def now(self) -> int:
        return 0

    def try_claim(self, hunt_id: str, fire: int, now: int | None = None) -> bool:
        return True

    def release(self, hunt_id: str, fire: int) -> None:
        pass

    @contextmanager
    def held(self, hunt_id: str, fire: int) -> Generator[None]:
        yield


def _hunts_dir(tmp_path: Path, yaml_text: str = HUNT_YAML) -> Path:
    hunt_dir = tmp_path / "hunts"
    hunt_dir.mkdir()
    (hunt_dir / "brute-force.yaml").write_text(yaml_text, encoding="utf-8")
    return hunt_dir


def _tick(tmp_path: Path, rows: int, webhook_url: str, yaml_text: str = HUNT_YAML) -> int:
    registry = AlertDestinationRegistry(directory=str(tmp_path / "destinations"))
    registry.add(AlertDestination(name="ops-webhook", url=webhook_url))
    try:
        specs = load_specs(_hunts_dir(tmp_path, yaml_text))
        coord = _Coordinator()
        worker = HuntWorker(
            _Ch(rows),
            coord,  # ty: ignore[invalid-argument-type]  # a stand-in carrying only the calls this path makes
            notifier=MatchNotifier(registry),
        )
        runner = HuntRunner(coord, worker, specs)  # ty: ignore[invalid-argument-type]  # a stand-in carrying only the calls this path makes
        return runner.tick(now=1_000_000)
    finally:
        registry.close()


def test_a_match_lands_at_the_webhook_destination(tmp_path):
    with _Webhook() as hook:
        executed = _tick(tmp_path, rows=5, webhook_url=hook.url)

    assert executed == 1
    assert len(hook.bodies) == 1
    assert "brute-force" in hook.bodies[0]["title"]
    assert "5" in hook.bodies[0]["message"]


@pytest.mark.parametrize(
    ("rows", "yaml_text", "delivered"),
    [
        pytest.param(0, HUNT_YAML, 0, id="no-rows-no-message"),
        pytest.param(2, HUNT_YAML, 0, id="under-the-trigger-threshold"),
        pytest.param(
            4,
            HUNT_YAML.split("alerts:")[0],
            0,
            id="hunt-names-no-destination",
        ),
        pytest.param(
            4,
            HUNT_YAML.replace("- ops-webhook", "- no-such-destination"),
            0,
            id="unknown-destination",
        ),
        pytest.param(
            4,
            HUNT_YAML.replace("result_count > 2", "result_count > 3"),
            1,
            id="trigger-met",
        ),
    ],
)
def test_only_a_fire_that_meets_the_hunts_triggers_sends(tmp_path, rows, yaml_text, delivered):
    with _Webhook() as hook:
        _tick(tmp_path, rows=rows, webhook_url=hook.url, yaml_text=yaml_text)

    assert len(hook.bodies) == delivered


def test_a_destination_that_cannot_be_reached_does_not_fail_the_fire(tmp_path):
    spec = HuntSpec(
        hunt_id="brute-force",
        interval_seconds=300,
        queries=["INSERT INTO dfe.detection SELECT 1 WHERE {window}"],
        alerts={"channels": ["json://127.0.0.1:1/hook"]},
    )
    coord = _Coordinator()
    worker = HuntWorker(
        _Ch(3),
        coord,  # ty: ignore[invalid-argument-type]  # a stand-in carrying only the calls this path makes
        notifier=MatchNotifier(),
    )

    worker.run(spec, scheduled_start=600)

    assert coord.watermark is not None


def test_a_dispatcher_that_raises_is_contained():
    def explode(config):
        raise RuntimeError("apprise broke")

    spec = HuntSpec(
        hunt_id="h",
        interval_seconds=60,
        alerts={"channels": ["json://example.invalid/x"]},
    )

    assert MatchNotifier(dispatcher_factory=explode).notify(spec, 3) is False


def test_global_channels_alert_a_hunt_with_no_alerts_block():
    sent: list[tuple[str, str]] = []

    class _Dispatcher(AlertDispatcher):
        def _send(self, title: str, body: str) -> bool:
            sent.append((title, body))
            return True

    spec = HuntSpec(hunt_id="h", interval_seconds=60)
    notifier = MatchNotifier(
        global_channels=["json://example.invalid/x"], dispatcher_factory=_Dispatcher
    )

    assert notifier.notify(spec, 4) is True
    assert len(sent) == 1
    assert "h" in sent[0][0]
    assert "4" in sent[0][1]
