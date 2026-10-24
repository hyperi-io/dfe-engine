#  Project:      dfe-engine
#  File:         ingest/models.py
#  Purpose:      Compatibility profiles, their deployment bindings and the ingest rule order
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The documents the ingest rule set compiles from, beside the sources.

:class:`CompatProfile` reads a profile the way dfe-schemas'
``compat-profiles/profile.schema.json`` defines it. Its rows stay in scalo.rules'
own format, which the receiver compile checks with scalo.rules itself.
:class:`CompatSetting` is the deployment's default bindings and :class:`RuleOrder`
the operator's order of the rule blocks. :class:`IngestRules` carries all three
into one compile.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dfe_engine.services.models.receiver import RULE_TARGETS
from dfe_engine.source.models import (
    COMPAT_PROFILE_ID_MAX_LENGTH,
    COMPAT_PROFILE_ID_PATTERN,
    SEMVER_PATTERN,
    CompatBinding,
)

HeaderField = Literal["_source", "_org_id", "_timestamp"]
"""The scalar header fields a profile may set (profile.schema.json ``$defs.header_field``)."""

ALIASES_TABLE = "aliases"
"""The lookup table a profile ships empty, which the engine fills from each source's aliases."""

_ID = re.compile(COMPAT_PROFILE_ID_PATTERN)


def _named(value: str, what: str) -> str:
    """Refuse *value* unless it is a profile-style name: lower-case words joined by hyphens."""
    if len(value) > COMPAT_PROFILE_ID_MAX_LENGTH or not _ID.match(value):
        raise ValueError(f"{what} {value!r} is not lower-case words joined by hyphens")
    return value


class CompatCover(BaseModel):
    """A sender a profile reads, shown as a chip."""

    model_config = ConfigDict(extra="forbid")

    platform: str = Field(description="The platform, e.g. dfe or elastic-agent")
    version: str | None = Field(
        default=None, min_length=1, description="The platform versions read, when only some"
    )

    @field_validator("platform")
    @classmethod
    def _platform_named(cls, v: str) -> str:
        return _named(v, "platform")


class CompatProfileVersion(BaseModel):
    """One published version of a profile: a complete snapshot that never changes."""

    model_config = ConfigDict(extra="forbid")

    date: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", description="Publication date")
    type: Literal["model", "addition", "revision"] = Field(
        description="SchemaVer: model is a major change, addition a minor one, revision a patch"
    )
    summary: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=80, description="The name the UI shows")
    description: str = Field(min_length=1, description="The UI tooltip: what it reads and sets")
    covers: list[CompatCover] = Field(min_length=1, description="The senders it reads")
    targets: list[HeaderField] = Field(
        min_length=1, description="The header fields its rows set, in evaluation order"
    )
    lookups: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description="Lookup tables its rows read. A shipped aliases table is empty",
    )
    rules: list[dict[str, Any]] = Field(
        min_length=1,
        description="The rows, in scalo.rules' format and in the order they fire",
    )
    opt_in: list[str] = Field(
        default_factory=list, description="Rows left out until a binding opts in to them"
    )

    @model_validator(mode="after")
    def _rows_well_formed(self) -> Self:
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("targets lists a field twice")
        for table, entries in self.lookups.items():
            _named(table, "lookup table")
            if any(not value for value in entries.values()):
                raise ValueError(f"lookups.{table} maps a key to empty text")
        ids: list[str] = []
        for index, row in enumerate(self.rules):
            row_id = row.get("id")
            if not isinstance(row_id, str):
                raise ValueError(f"rules[{index}] has no id")
            ids.append(_named(row_id, "row id"))
            if "match" in row and "when" in row:
                raise ValueError(f"row {row_id!r} has both match and when")
            settings = row.get("set")
            if not isinstance(settings, dict) or len(settings) != 1:
                raise ValueError(f"row {row_id!r} sets exactly one header field")
            target = next(iter(settings))
            if target not in self.targets:
                raise ValueError(f"row {row_id!r} sets {target!r}, which targets does not list")
        if len(set(ids)) != len(ids):
            raise ValueError("a row id is used twice")
        unknown = sorted(set(self.opt_in) - set(ids))
        if unknown or len(set(self.opt_in)) != len(self.opt_in):
            raise ValueError(f"opt_in names a row twice or one that is not listed: {unknown}")
        return self

    def row_ids(self) -> list[str]:
        """Every row's id, in declared order."""
        return [str(row["id"]) for row in self.rules]

    @staticmethod
    def row_target(row: Mapping[str, Any]) -> str:
        """The header field a row sets."""
        return next(iter(row["set"]))

    def unsupported_targets(self) -> list[str]:
        """The fields this version sets that dfe-receiver's rule set does not assign."""
        return [target for target in self.targets if target not in RULE_TARGETS]


class CompatProfile(BaseModel):
    """A compatibility profile: versioned rule rows reading another platform's header."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="The profile's id, its file name without .yaml")
    resource_type: Literal["core", "custom"] = Field(
        description="core ships in dfe-schemas and refuses edits, custom is an operator's"
    )
    current: str = Field(
        pattern=SEMVER_PATTERN, description="The version a binding takes by default"
    )
    versions: dict[str, CompatProfileVersion] = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def _id_named(cls, v: str) -> str:
        return _named(v, "profile id")

    @model_validator(mode="after")
    def _current_published(self) -> Self:
        for version in self.versions:
            if not re.match(SEMVER_PATTERN, version):
                raise ValueError(f"version {version!r} is not x.y.z")
        if self.current not in self.versions:
            raise ValueError(f"current {self.current!r} is not one of the versions")
        return self

    def resolve(self, version: str | None) -> str:
        """The version a binding naming *version* takes: its own, else ``current``."""
        return version or self.current

    def entry(self, version: str | None) -> CompatProfileVersion:
        """The snapshot a binding naming *version* reads.

        Raises:
            KeyError: The profile publishes no such version.
        """
        return self.versions[self.resolve(version)]


class CompatSetting(BaseModel):
    """The profiles every source binds unless it names its own list."""

    model_config = ConfigDict(extra="forbid")

    profiles: list[CompatBinding] = Field(
        default_factory=list, description="The deployment's default bindings"
    )

    @field_validator("profiles")
    @classmethod
    def _bound_once(cls, v: list[CompatBinding]) -> list[CompatBinding]:
        names = [binding.profile for binding in v]
        twice = sorted({name for name in names if names.count(name) > 1})
        if twice:
            raise ValueError(f"compatibility profile bound twice: {', '.join(twice)}")
        return v


class RuleOrder(BaseModel):
    """The operator's order of the ingest rule blocks, per target field.

    A block the order does not name runs after every block it names: sources by
    name, then profiles by id.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)

    source: list[str] = Field(
        default_factory=list, alias="_source", description="Block ids for _source, in order"
    )
    org_id: list[str] = Field(
        default_factory=list, alias="_org_id", description="Block ids for _org_id, in order"
    )

    def ids(self, target: str) -> list[str]:
        """The block ids this order names for *target*."""
        return list(self.source if target == RULE_TARGETS[0] else self.org_id)

    def with_ids(self, target: str, ids: list[str]) -> RuleOrder:
        """This order with *target*'s block ids replaced by *ids*."""
        key = "source" if target == RULE_TARGETS[0] else "org_id"
        return self.model_copy(update={key: list(ids)})


@dataclass(frozen=True, slots=True)
class IngestRules:
    """Everything one rule-set compile reads beside the sources.

    Attributes:
        order: The operator's block order per target.
        profiles: Every compatibility profile stored, by id.
        defaults: The bindings a source with no list of its own takes.
    """

    order: RuleOrder = field(default_factory=RuleOrder)
    profiles: Mapping[str, CompatProfile] = field(default_factory=dict)
    defaults: tuple[CompatBinding, ...] = ()
