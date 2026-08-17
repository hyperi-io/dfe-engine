#  Project:      dfe-engine
#  File:         tests/unit/test_synthetic_data/test_sample_source.py
#  Purpose:      Lookalike generation: shape kept, identities never replayed
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from dfe_engine.synthetic_data.models import SyntheticDataError
from dfe_engine.synthetic_data.sample_source import SampleEventFactory

FIXED_END = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)

SAMPLE_USERS = ("derek.real", "kaz.real", "damien.real")
SAMPLE_PRIVATE_IPS = ("192.168.7.10", "192.168.7.11", "192.168.7.12")
SAMPLE_PUBLIC_IPS = ("203.0.113.5", "203.0.113.9")
SAMPLE_UUIDS = (
    "11111111-2222-3333-4444-555555555555",
    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
)


def sample_rows(n: int = 60) -> list[dict]:
    rows = []
    for i in range(n):
        rows.append(
            {
                "timestamp": f"2026-08-18T09:{i % 60:02d}:00.000Z",
                "user": SAMPLE_USERS[i % 3],
                "action": "login" if i % 4 else "logout",
                "bytes": 100 + i * 7,
                "session_id": SAMPLE_UUIDS[i % 2],
                "source": {"ip": SAMPLE_PRIVATE_IPS[i % 3]},
                "message": (
                    f"Failed password for {SAMPLE_USERS[i % 3]} "
                    f"from {SAMPLE_PUBLIC_IPS[i % 2]} port {2000 + i} ssh2"
                ),
            }
        )
    return rows


@pytest.fixture
def events() -> list[dict]:
    factory = SampleEventFactory(rows=sample_rows(), seed=42)
    return factory.events(80, end=FIXED_END)


class TestLookalikeShape:
    def test_events_keep_the_sample_shape(self, events):
        for event in events:
            json.dumps(event)
            assert set(event) >= {
                "timestamp",
                "user",
                "action",
                "bytes",
                "session_id",
                "source",
                "message",
                "tags",
            }
            assert event["tags"]["synthetic"] is True
            assert event["timestamp"].endswith("Z")

    def test_enum_vocabulary_replayed(self, events):
        actions = {e["action"] for e in events}
        assert actions == {"login", "logout"}

    def test_numeric_range_fitted(self, events):
        values = [e["bytes"] for e in events]
        assert min(values) >= 100
        assert max(values) <= 100 + 59 * 7

    def test_message_structure_kept(self, events):
        for event in events:
            assert event["message"].startswith("Failed password for ")
            assert event["message"].endswith("ssh2")


class TestIdentityScrub:
    def test_ips_never_replayed(self, events):
        sample_ips = set(SAMPLE_PRIVATE_IPS) | set(SAMPLE_PUBLIC_IPS)
        for event in events:
            assert event["source"]["ip"] not in sample_ips

    def test_usernames_never_replayed(self, events):
        for event in events:
            assert event["user"] not in SAMPLE_USERS

    def test_uuids_never_replayed(self, events):
        for event in events:
            assert event["session_id"] not in SAMPLE_UUIDS

    def test_free_text_scrubbed(self, events):
        for event in events:
            message = event["message"]
            for ip in (*SAMPLE_PRIVATE_IPS, *SAMPLE_PUBLIC_IPS):
                assert ip not in message


class TestModesAndEdges:
    def test_lines_mode_scrubs_messages(self):
        lines = [
            f"Accepted password for {user} from {ip} port 22 ssh2"
            for user in SAMPLE_USERS
            for ip in SAMPLE_PUBLIC_IPS
        ]
        factory = SampleEventFactory(lines=lines, seed=7)
        events = factory.events(30, end=FIXED_END)
        for event in events:
            assert event["tags"]["synthetic"] is True
            assert event["message"].startswith("Accepted password for ")
            for ip in SAMPLE_PUBLIC_IPS:
                assert ip not in event["message"]

    def test_epoch_ms_timestamps_detected(self):
        rows = [{"ts": 1755500000000 + i * 1000, "level": "info"} for i in range(30)]
        factory = SampleEventFactory(rows=rows, seed=3)
        event = factory.event(when=FIXED_END)
        assert isinstance(event["ts"], int)
        assert event["ts"] == int(FIXED_END.timestamp() * 1000)

    def test_deterministic(self):
        a = SampleEventFactory(rows=sample_rows(), seed=9).events(20, end=FIXED_END)
        b = SampleEventFactory(rows=sample_rows(), seed=9).events(20, end=FIXED_END)
        assert a == b

    def test_empty_input_rejected(self):
        with pytest.raises(SyntheticDataError, match=r"(?i)sample"):
            SampleEventFactory(rows=[], lines=[])

    def test_count_below_one_rejected(self):
        factory = SampleEventFactory(rows=sample_rows(10), seed=1)
        with pytest.raises(SyntheticDataError, match="count"):
            factory.events(0)
