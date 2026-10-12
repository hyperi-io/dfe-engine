#  Project:      dfe-engine
#  File:         tests/unit/test_admin_links.py
#  Purpose:      The deployer's admin-UI list: what survives validation, and how a probe reads
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The admin-UI list the deployer injects, and the probes that answer up or down.

Parsing is checked against a real scalo metrics manager, so a dropped entry is
asserted on the counter the engine serves. Probes go to a real stdlib HTTP
server on localhost, so each status is read off a real socket round trip.
"""

import asyncio
import json
import socket
import threading
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from prometheus_client.parser import text_string_to_metric_families
from scalo.metrics import create_metrics

from dfe_engine.admin_links import (
    LINKS_DROPPED,
    AdminLink,
    AdminLinkMetrics,
    AdminLinks,
    AdminLinkStatus,
    parse_links,
)
from tests.support.loopback import stop_server

ARGO = {
    "name": "Argo CD",
    "purpose": "Sync state of every app",
    "url": "https://argocd.dfe.example.com",
    "probe_url": "http://argocd-server.argocd.svc",
}
KAFBAT = {
    "name": "Kafbat",
    "purpose": "Topics and consumer lag",
    "url": "https://kafbat.example.com",
}


def _manager():
    return create_metrics("test", backend="prometheus", enable_auto_update=False, metric_prefix="")


def _dropped(manager) -> dict[str, float]:
    counts: dict[str, float] = {}
    for family in text_string_to_metric_families(manager.metrics_text):
        for sample in family.samples:
            if sample.name == LINKS_DROPPED:
                counts[sample.labels["reason"]] = sample.value
    return counts


class TestParse:
    def test_a_json_array_from_the_env_var_is_read_in_order(self):
        links = parse_links(json.dumps([ARGO, KAFBAT]), AdminLinkMetrics())

        assert [link.name for link in links] == ["Argo CD", "Kafbat"]
        assert links[0].probe_url == "http://argocd-server.argocd.svc"
        assert links[1].probe_url is None

    def test_a_list_from_a_config_file_is_read_the_same(self):
        links = parse_links([ARGO], AdminLinkMetrics())

        assert links == [AdminLink.model_validate(ARGO)]

    @pytest.mark.parametrize("raw", ["", "   ", "[]", []])
    def test_nothing_listed_is_no_links_and_nothing_dropped(self, raw):
        manager = _manager()

        assert parse_links(raw, AdminLinkMetrics(manager)) == []
        assert _dropped(manager) == {}

    @pytest.mark.parametrize("raw", ["[{", "not json", '{"name": "Argo CD"}', '"a string"', "7"])
    def test_a_value_that_is_not_a_json_array_is_dropped_whole(self, raw):
        manager = _manager()

        assert parse_links(raw, AdminLinkMetrics(manager)) == []
        assert _dropped(manager) == {"unparseable": 1.0}

    def test_an_entry_that_is_not_an_object_is_dropped_and_the_rest_kept(self):
        manager = _manager()

        links = parse_links(json.dumps(["Argo CD", ARGO, 3]), AdminLinkMetrics(manager))

        assert [link.name for link in links] == ["Argo CD"]
        assert _dropped(manager) == {"not_a_mapping": 2.0}

    @pytest.mark.parametrize(
        "broken",
        [
            {k: v for k, v in ARGO.items() if k != "name"},
            {k: v for k, v in ARGO.items() if k != "purpose"},
            {k: v for k, v in ARGO.items() if k != "url"},
            {**ARGO, "name": "   "},
            {**ARGO, "url": "argocd.example.com"},
            {**ARGO, "url": "/argocd"},
            {**ARGO, "url": "ftp://argocd.example.com"},
            {**ARGO, "url": "https://argocd.example.com:notaport"},
            {**ARGO, "url": "https://argocd.example.com:0"},
            {**ARGO, "url": "https://admin:hunter2@argocd.example.com"},
            {**ARGO, "probe_url": "http://admin:hunter2@argocd-server.argocd.svc"},
            {**ARGO, "probe_url": "argocd-server.argocd.svc"},
            # A misspelt key would otherwise leave the link unprobed with no sign why.
            {**ARGO, "probeurl": "http://argocd-server.argocd.svc"},
        ],
        ids=[
            "no-name",
            "no-purpose",
            "no-url",
            "blank-name",
            "url-no-scheme",
            "url-relative",
            "url-not-http",
            "url-bad-port",
            "url-port-zero",
            "url-credentials",
            "probe-credentials",
            "probe-no-scheme",
            "unknown-key",
        ],
    )
    def test_an_invalid_entry_is_dropped_and_the_rest_kept(self, broken):
        manager = _manager()

        links = parse_links([broken, KAFBAT], AdminLinkMetrics(manager))

        assert [link.name for link in links] == ["Kafbat"]
        assert _dropped(manager) == {"invalid": 1.0}

    def test_the_drop_log_names_the_field_but_never_the_password(self, caplog_loguru):
        parse_links(
            [{**ARGO, "url": "https://admin:hunter2@argocd.example.com"}], AdminLinkMetrics()
        )

        [event] = caplog_loguru
        assert event["reason"] == "invalid"
        assert event["name"] == "Argo CD"
        assert "url" in event["error"]
        assert "hunter2" not in str(event)

    def test_surrounding_whitespace_is_trimmed(self):
        [link] = parse_links([{**ARGO, "name": "  Argo CD  "}], AdminLinkMetrics())

        assert link.name == "Argo CD"


@pytest.fixture
def caplog_loguru() -> Iterator[list[dict]]:
    """Each warning the module logs, as its message plus structured fields."""
    from scalo.logger import logger

    events: list[dict] = []

    def record(message) -> None:
        events.append({"event": message.record["message"], **message.record["extra"]})

    handler = logger.add(record, level="WARNING", format="{message}")
    try:
        yield events
    finally:
        logger.remove(handler)


# -- Probes ---------------------------------------------------------------


@dataclass
class _Console:
    """A local HTTP server answering each path with a fixed status."""

    base_url: str
    httpd: ThreadingHTTPServer
    thread: threading.Thread
    hits: Counter = field(default_factory=Counter)

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"


# Two concurrent probes each wait here for the other; a serial prober never gets past it.
_PAIR = threading.Barrier(2, timeout=5.0)


def _serve() -> _Console:
    console: _Console

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self):
            console.hits[self.path] += 1
            if self.path == "/slow":
                time.sleep(2.0)
            if self.path.startswith("/pair"):
                try:
                    _PAIR.wait()
                except threading.BrokenBarrierError:
                    self.send_response(500)
                    self.end_headers()
                    return
            status = {"/login": 302, "/auth": 401, "/missing": 404, "/broken": 503}.get(
                self.path, 200
            )
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/sso/start")
            self.send_header("Content-Length", "0")
            self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    console = _Console(base_url=f"http://127.0.0.1:{httpd.server_port}", httpd=httpd, thread=thread)
    return console


@pytest.fixture
def console() -> Iterator[_Console]:
    server = _serve()
    yield server
    stop_server(server.httpd, server.thread)


def _closed_port() -> int:
    """A localhost port nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _link(name: str, probe_url: str | None) -> AdminLink:
    return AdminLink(name=name, purpose="p", url=f"https://{name}.example.com", probe_url=probe_url)


class TestProbe:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/ok", AdminLinkStatus.UP),
            ("/login", AdminLinkStatus.UP),
            ("/auth", AdminLinkStatus.UP),
            ("/missing", AdminLinkStatus.UP),
            ("/broken", AdminLinkStatus.DOWN),
        ],
    )
    async def test_any_answer_below_500_is_up(self, console, path, expected):
        links = AdminLinks([_link("ui", console.url(path))])

        [(_, status)] = await links.statuses()

        assert status is expected
        assert console.hits[path] == 1

    async def test_nothing_listening_is_down(self):
        links = AdminLinks([_link("ui", f"http://127.0.0.1:{_closed_port()}/")])

        [(_, status)] = await links.statuses()

        assert status is AdminLinkStatus.DOWN

    async def test_a_ui_slower_than_the_timeout_is_down(self, console):
        links = AdminLinks([_link("ui", console.url("/slow"))], timeout=0.3)

        [(_, status)] = await links.statuses()

        assert status is AdminLinkStatus.DOWN

    async def test_a_link_with_no_probe_address_is_unknown(self, console):
        links = AdminLinks([_link("a", None), _link("b", console.url("/ok"))])

        statuses = [status for _, status in await links.statuses()]

        assert statuses == [AdminLinkStatus.UNKNOWN, AdminLinkStatus.UP]

    async def test_no_links_is_an_empty_answer(self):
        assert await AdminLinks([]).statuses() == []

    async def test_the_probes_run_at_the_same_time(self, console):
        _PAIR.reset()
        links = AdminLinks(
            [_link("a", console.url("/pair-a")), _link("b", console.url("/pair-b"))], timeout=4.0
        )

        statuses = [status for _, status in await links.statuses()]

        assert statuses == [AdminLinkStatus.UP, AdminLinkStatus.UP]

    async def test_a_refresh_inside_the_ttl_sends_no_new_probe(self, console):
        links = AdminLinks([_link("ui", console.url("/ok"))], ttl=60.0)

        await links.statuses()
        await links.statuses()

        assert console.hits["/ok"] == 1

    async def test_requests_arriving_together_share_one_round(self, console):
        links = AdminLinks([_link("ui", console.url("/ok"))], ttl=60.0)

        await asyncio.gather(links.statuses(), links.statuses(), links.statuses())

        assert console.hits["/ok"] == 1

    async def test_an_expired_round_probes_again(self, console):
        links = AdminLinks([_link("ui", console.url("/ok"))], ttl=0.0)

        await links.statuses()
        await links.statuses()

        assert console.hits["/ok"] == 2
