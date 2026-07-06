#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_cloud.py
#  Purpose:      Unit tests for the CH Cloud service-lifecycle client
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""CloudService tests - a fake HTTP client, no network, deterministic clock."""

from __future__ import annotations

import pytest

from dfe_engine.clickhouse.cloud import CloudService, CloudServiceError
from dfe_engine.settings import ClickHouseCloudSettings


class _Resp:
    def __init__(self, data: dict) -> None:
        self._data = data

    def json(self) -> dict:
        return self._data


class FakeHttp:
    """Context-manager HTTP double: canned service state, records PATCHes."""

    def __init__(self, states: list[str], *, sid: str = "svc-1", name: str = "dfe") -> None:
        self._states = list(states)
        self.sid = sid
        self.name = name
        self.patched: list[tuple[str, dict]] = []

    def __enter__(self) -> FakeHttp:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def _state(self) -> str:
        return self._states.pop(0) if len(self._states) > 1 else self._states[0]

    def get(self, path: str) -> _Resp:
        if path == "/organizations":
            return _Resp({"result": [{"id": "org-1"}]})
        if path.endswith("/services"):
            return _Resp({"result": [{"id": self.sid, "name": self.name, "state": self._state()}]})
        raise AssertionError(f"unexpected GET {path}")

    def patch(self, path: str, json: dict) -> _Resp:
        self.patched.append((path, json))
        return _Resp({"result": {"state": json["command"] + "ing"}})


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _cfg(**kw) -> ClickHouseCloudSettings:
    base = {"api_key_id": "k", "api_key_secret": "s", "service_name": "dfe"}
    base.update(kw)
    return ClickHouseCloudSettings(**base)


def _service(fake: FakeHttp, cfg: ClickHouseCloudSettings | None = None) -> CloudService:
    clock = FakeClock()
    svc = CloudService(cfg or _cfg(), http_factory=lambda: fake, sleep=clock.sleep, now=clock.now)
    svc._clock = clock  # type: ignore[attr-defined]
    return svc


class TestStatus:
    def test_status_resolves_by_name(self):
        svc = _service(FakeHttp(["running"]))
        st = svc.status()
        assert st.state == "running"
        assert st.is_running
        assert not st.is_stopped

    def test_status_resolves_by_id(self):
        svc = _service(FakeHttp(["stopped"], sid="the-id"), _cfg(service_id="the-id"))
        st = svc.status()
        assert st.id == "the-id"
        assert st.is_stopped

    def test_service_not_found_raises(self):
        svc = _service(FakeHttp(["running"], name="other"))
        with pytest.raises(CloudServiceError, match="not found"):
            svc.status()


class TestStartStop:
    def test_start_from_stopped_issues_start(self):
        fake = FakeHttp(["stopped", "starting"])
        svc = _service(fake)
        svc.start()
        assert fake.patched == [("/organizations/org-1/services/svc-1/state", {"command": "start"})]

    def test_start_when_running_is_noop(self):
        fake = FakeHttp(["running"])
        svc = _service(fake)
        svc.start()
        assert fake.patched == []  # already running -> no billable start issued

    def test_stop_from_running_issues_stop(self):
        fake = FakeHttp(["running", "stopping"])
        svc = _service(fake)
        svc.stop()
        assert fake.patched == [("/organizations/org-1/services/svc-1/state", {"command": "stop"})]

    def test_stop_when_stopped_is_noop(self):
        fake = FakeHttp(["stopped"])
        svc = _service(fake)
        svc.stop()
        assert fake.patched == []


class TestWaitRunning:
    def test_polls_until_running(self):
        fake = FakeHttp(["starting", "starting", "running"])
        svc = _service(fake)
        st = svc.wait_running(timeout=600.0, poll_interval=10.0)
        assert st.is_running

    def test_times_out(self):
        fake = FakeHttp(["starting"])  # never becomes running
        svc = _service(fake)
        with pytest.raises(CloudServiceError, match="not running"):
            svc.wait_running(timeout=30.0, poll_interval=10.0)


class TestConfigured:
    def test_unconfigured_default_factory_raises(self):
        # No creds -> the real factory refuses (never silently no-ops).
        svc = CloudService(ClickHouseCloudSettings())
        with pytest.raises(CloudServiceError, match="not configured"):
            svc.status()
