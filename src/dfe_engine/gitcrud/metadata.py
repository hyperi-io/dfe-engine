#  Project:      dfe-engine
#  File:         gitcrud/metadata.py
#  Purpose:      Universal descriptive metadata for any gitcrud resource
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A standard, universal ``metadata`` block for ANY gitcrud resource.

k8s-style descriptive metadata, baked into the gitcrud file convention so the CLI/UI
list, filter, and document every resource type - source definitions, helmvars,
governance, sigma rules, remap views - through ONE convention rather than a per-class
add-on. Purely DESCRIPTIVE: the version envelope (``gitcrud.versioned``) owns lifecycle
(who/when/message), this owns human/UI help (what/why/how-to-find).

A resource file carries it under a top-level ``metadata`` key::

    metadata:
      description: "Filebeat AWS module - CloudTrail, VPC Flow, GuardDuty..."
      display_name: "Filebeat: AWS"
      labels: {beat: filebeat, vendor: aws, category: cloud}
      tags: [security, cloud, aws]
      docs: "https://.../filebeat_aws"
    <resource body>
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

METADATA_KEY = "metadata"


class ResourceMetadata(BaseModel):
    """Descriptive metadata common to every gitcrud resource.

    - ``description`` - a one-line human summary (the primary UI/CLI help).
    - ``display_name`` - a friendly name for UI lists (falls back to the resource key).
    - ``labels`` - single-value, FILTERABLE key/values (``vendor: aws``).
    - ``tags`` - free-form tags for search/grouping (``security``, ``cloud``).
    - ``docs`` - a docs URL/reference.
    """

    model_config = ConfigDict(extra="allow")  # forward-compatible: unknown meta kept

    description: str = ""
    display_name: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    docs: str | None = None

    def matches(self, *, label: tuple[str, str] | None = None, tag: str | None = None) -> bool:
        """Whether this resource matches a UI/CLI filter (label k=v and/or a tag)."""
        if label is not None and self.labels.get(label[0]) != label[1]:
            return False
        return tag is None or tag in self.tags


def extract_metadata(doc: dict[str, Any]) -> ResourceMetadata:
    """Read the ``metadata`` block from a gitcrud doc (empty metadata if absent)."""
    raw = doc.get(METADATA_KEY) if isinstance(doc, dict) else None
    if isinstance(raw, dict):
        return ResourceMetadata.model_validate(raw)
    return ResourceMetadata()


def with_metadata(doc: dict[str, Any], metadata: ResourceMetadata) -> dict[str, Any]:
    """Return ``doc`` with its ``metadata`` block set (non-empty fields only), so the
    stored file stays clean."""
    out = dict(doc)
    # exclude_defaults (not exclude_none + empty-filter) so unknown EXTRA keys
    # survive the round-trip while defaulted standard fields stay off disk.
    meta = metadata.model_dump(exclude_defaults=True)
    if meta:
        out[METADATA_KEY] = meta
    else:
        out.pop(METADATA_KEY, None)
    return out
