#  Project:      dfe-engine
#  File:         tests/unit/test_datagen/test_schema_source.py
#  Purpose:      @source path inversion, factory generation, datagen hints
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dfe_engine.datagen.models import DatagenError
from dfe_engine.datagen.schema_source import (
    SchemaEventFactory,
    load_datagen_hints,
    parse_source_path,
    set_path,
)
from dfe_engine.source.models import SchemaColumn

REPO_ROOT = Path(__file__).resolve().parents[3]
GUARDDUTY = REPO_ROOT / "schemas" / "meta" / "aws" / "guardduty.yaml"

FIXED_END = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)


def seg_tuples(expr: str):
    segments = parse_source_path(expr)
    return None if segments is None else [(s.key, s.index) for s in segments]


class TestParseSourcePath:
    def test_plain_key(self):
        assert seg_tuples("@source: AccountId") == [("AccountId", None)]

    def test_key_with_spaces(self):
        assert seg_tuples("@source: Report Refresh Date") == [("Report Refresh Date", None)]

    def test_dotted_path(self):
        assert seg_tuples("@source: protoPayload.authenticationInfo.principalEmail") == [
            ("protoPayload", None),
            ("authenticationInfo", None),
            ("principalEmail", None),
        ]

    def test_array_index(self):
        assert seg_tuples("@source: Resources[0].Type") == [("Resources", 0), ("Type", None)]

    def test_first_takes_leading_alternative(self):
        assert seg_tuples("@source: first(CreatedAt/createdAt)") == [("CreatedAt", None)]
        assert seg_tuples(
            "@source: first(properties.timeGeneratedUtc/properties.startTimeUtc)"
        ) == [
            ("properties", None),
            ("timeGeneratedUtc", None),
        ]

    def test_non_source_exprs_ignored(self):
        assert parse_source_path("@generated: now64(3)") is None
        assert parse_source_path("") is None
        assert parse_source_path(None) is None

    def test_malformed_segment_raises(self):
        with pytest.raises(DatagenError):
            parse_source_path("@source: Resources[x].Type")


class TestSetPath:
    def test_nested_and_indexed(self):
        event: dict = {}
        set_path(event, parse_source_path("@source: Resource.ResourceType"), "AwsEc2Instance")
        set_path(event, parse_source_path("@source: Resources[0].Type"), "AwsIamUser")
        set_path(event, parse_source_path("@source: Id"), "abc")
        assert event["Resource"]["ResourceType"] == "AwsEc2Instance"
        assert event["Resources"][0]["Type"] == "AwsIamUser"
        assert event["Id"] == "abc"


@pytest.mark.skipif(not GUARDDUTY.exists(), reason="dfe-schemas submodule not checked out")
class TestFactoryFromRealSchema:
    def test_events_are_source_shaped_and_valid(self):
        factory = SchemaEventFactory.from_schema(GUARDDUTY, seed=42)
        events = factory.events(200, end=FIXED_END)
        assert len(events) == 200
        regions = {e["Region"] for e in events}
        for event in events:
            json.dumps(event)  # everything must serialise
            assert set(event) >= {"Id", "Type", "Severity", "Region", "AccountId", "tags"}
            assert event["Resource"]["ResourceType"]
            assert 0.0 <= event["Severity"] <= 10.0
            assert event["CreatedAt"].endswith("Z")
        # A 200-event stream should show entity variety, not one flat value.
        assert len(regions) > 1

    def test_synthetic_tag_default_on(self):
        factory = SchemaEventFactory.from_schema(GUARDDUTY, seed=1)
        assert factory.event(when=FIXED_END)["tags"]["synthetic"] is True

    def test_synthetic_tag_overridable(self):
        factory = SchemaEventFactory.from_schema(GUARDDUTY, seed=1, mark_synthetic=False)
        assert "synthetic" not in factory.event(when=FIXED_END).get("tags", {})

    def test_extra_tags_merged(self):
        factory = SchemaEventFactory.from_schema(GUARDDUTY, seed=1, tags={"scenario": "demo-1"})
        tags = factory.event(when=FIXED_END)["tags"]
        assert tags["scenario"] == "demo-1"
        assert tags["synthetic"] is True

    def test_identical_seed_identical_events(self):
        a = SchemaEventFactory.from_schema(GUARDDUTY, seed=99).events(50, end=FIXED_END)
        b = SchemaEventFactory.from_schema(GUARDDUTY, seed=99).events(50, end=FIXED_END)
        assert a == b

    def test_batch_timestamps_ascend(self):
        events = SchemaEventFactory.from_schema(GUARDDUTY, seed=5).events(50, end=FIXED_END)
        stamps = [e["CreatedAt"] for e in events]
        assert stamps == sorted(stamps)
        assert stamps[-1] <= "2026-08-18T12:00:00.001Z"


HINTED_SCHEMA = """
current: "1.0.0"
versions:
  "1.0.0":
    date: "2026-08-18"
    type: model
    summary: "datagen hints fixture"
    columns:
      - name: vendor
        type: string
        expr: "@source: vendor"
        datagen:
          static: acme
      - name: action
        type: string
        expr: "@source: action"
        datagen:
          values: [allow, deny]
          weights: [9, 1]
      - name: message
        type: text
        expr: "@source: message"
        datagen:
          templates:
            - "Failed password for {username} from {external_ipv4} port {port} ssh2"
      - name: score
        type: integer
        expr: "@source: score"
        datagen:
          minimum: 1
          maximum: 5
      - name: event_time
        type: string
        expr: "@source: event_time"
        datagen:
          format: epoch_ms
      - name: agent
        type: string
        expr: "@source: agent"
        datagen:
          provider: user_agent
"""


@pytest.fixture
def hinted_schema(tmp_path: Path) -> Path:
    path = tmp_path / "hinted.yaml"
    path.write_text(HINTED_SCHEMA, encoding="utf-8")
    return path


class TestHints:
    def test_hints_loaded(self, hinted_schema: Path):
        hints = load_datagen_hints(hinted_schema)
        assert set(hints) == {"vendor", "action", "message", "score", "event_time", "agent"}

    def test_hint_behaviours(self, hinted_schema: Path):
        factory = SchemaEventFactory.from_schema(hinted_schema, seed=3)
        events = factory.events(100, end=FIXED_END)
        actions = {e["action"] for e in events}
        for event in events:
            assert event["vendor"] == "acme"
            assert event["action"] in {"allow", "deny"}
            assert 1 <= event["score"] <= 5
            assert isinstance(event["event_time"], int)
            assert "Failed password for " in event["message"]
            assert "{" not in event["message"], "template placeholder left unfilled"
            assert event["agent"]
        assert actions == {"allow", "deny"}
        # 9:1 weighting must actually skew the draw.
        assert sum(e["action"] == "allow" for e in events) > 60

    def test_unknown_placeholder_raises(self, tmp_path: Path):
        bad = HINTED_SCHEMA.replace("{username}", "{nonexistent}")
        path = tmp_path / "bad.yaml"
        path.write_text(bad, encoding="utf-8")
        factory = SchemaEventFactory.from_schema(path, seed=3)
        with pytest.raises(DatagenError, match="placeholder"):
            factory.event(when=FIXED_END)

    def test_unknown_provider_raises(self, tmp_path: Path):
        bad = HINTED_SCHEMA.replace("provider: user_agent", "provider: not_a_provider")
        path = tmp_path / "bad.yaml"
        path.write_text(bad, encoding="utf-8")
        factory = SchemaEventFactory.from_schema(path, seed=3)
        with pytest.raises(DatagenError, match="provider"):
            factory.event(when=FIXED_END)

    def test_mismatched_weights_raise(self, tmp_path: Path):
        bad = HINTED_SCHEMA.replace("weights: [9, 1]", "weights: [9]")
        path = tmp_path / "bad.yaml"
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(DatagenError, match="weights"):
            load_datagen_hints(path)


class TestFactoryEdges:
    def test_no_source_columns_raises(self):
        columns = [SchemaColumn(name="x", type="string", expr="@generated: now64(3)")]
        with pytest.raises(DatagenError, match="no @source"):
            SchemaEventFactory(columns, seed=1)

    def test_count_below_one_raises(self):
        columns = [SchemaColumn(name="x", type="string", expr="@source: x")]
        factory = SchemaEventFactory(columns, seed=1)
        with pytest.raises(DatagenError, match="count"):
            factory.events(0)
