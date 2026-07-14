#  Project:      dfe-engine
#  File:         tests/gitcrud/test_metadata.py
#  Purpose:      Universal gitcrud resource metadata (description/labels/tags)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The standard descriptive-metadata block carried by ANY gitcrud resource."""

from __future__ import annotations

from dfe_engine.gitcrud.metadata import (
    METADATA_KEY,
    ResourceMetadata,
    extract_metadata,
    with_metadata,
)


def test_extract_absent_metadata_is_empty() -> None:
    meta = extract_metadata({"spec": {"a": 1}})
    assert meta.description == ""
    assert meta.labels == {}
    assert meta.tags == []


def test_roundtrip_extract_and_attach() -> None:
    m = ResourceMetadata(
        description="Filebeat AWS module",
        display_name="Filebeat: AWS",
        labels={"beat": "filebeat", "vendor": "aws"},
        tags=["security", "cloud"],
    )
    doc = with_metadata({"spec": {"x": 1}}, m)
    assert doc[METADATA_KEY]["description"] == "Filebeat AWS module"
    assert doc["spec"] == {"x": 1}  # body untouched
    back = extract_metadata(doc)
    assert back.labels == {"beat": "filebeat", "vendor": "aws"}
    assert back.tags == ["security", "cloud"]


def test_with_metadata_omits_empty_and_can_clear() -> None:
    # empty metadata -> no metadata key written (clean file)
    doc = with_metadata({"spec": {}}, ResourceMetadata())
    assert METADATA_KEY not in doc
    # setting then clearing removes the block
    doc = with_metadata({METADATA_KEY: {"description": "old"}, "spec": {}}, ResourceMetadata())
    assert METADATA_KEY not in doc


def test_matches_label_and_tag_filters() -> None:
    m = ResourceMetadata(labels={"vendor": "aws"}, tags=["cloud"])
    assert m.matches(label=("vendor", "aws"))
    assert not m.matches(label=("vendor", "azure"))
    assert m.matches(tag="cloud")
    assert not m.matches(tag="onprem")
    assert m.matches(label=("vendor", "aws"), tag="cloud")


def test_extra_metadata_is_forward_compatible() -> None:
    # unknown future keys are preserved, not rejected
    meta = extract_metadata({METADATA_KEY: {"description": "d", "future_key": "keep"}})
    assert meta.description == "d"
    assert meta.model_dump().get("future_key") == "keep"


def test_with_metadata_round_trips_extra_keys() -> None:
    # extras survive a read-modify-write cycle (with_metadata must not drop them)
    meta = extract_metadata({METADATA_KEY: {"description": "d", "future_key": "keep"}})
    doc = with_metadata({"spec": {}}, meta)
    assert doc[METADATA_KEY]["description"] == "d"
    assert doc[METADATA_KEY]["future_key"] == "keep"
