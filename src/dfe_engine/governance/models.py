#  Project:      dfe-engine
#  File:         governance/models.py
#  Purpose:      Models for Governed Ops defined actions + protected-var policies
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Pydantic models for defined actions and protected-var policies (YAML in gitops).

The stringly fields here are all closed sets, so each carries an
``x-dfe-enum-source`` schema annotation naming the endpoint that enumerates its
legal values (contract rule: if the server will reject values outside a set,
the contract must expose the set). ``params`` in the endpoint template refer to
sibling fields of the same object.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


def param_ref(value: Any) -> tuple[str, dict[str, Any] | None] | None:
    """Return (param_name, map|None) when ``value`` is a param reference.

    A reference is exactly ``{"$param": <name>}`` or ``{"$param": <name>,
    "map": {...}}`` - whole-value substitution only. A dict without ``$param``
    is an ordinary subtree literal. Params never reach cls/name/path: no
    substitution is ever performed there, by construction.
    """
    if isinstance(value, dict) and "$param" in value:
        return value["$param"], value.get("map")
    return None


class ParamSpec(BaseModel):
    """A constrained invoke-time parameter.

    Every param carries a CLOSED constraint - an enum carries its full value
    list, a numeric carries both bounds. There is deliberately NO free-string
    type: an unconstrained param would reopen the hole curation closed.
    """

    type: Literal["enum", "int", "float"]
    values: list[str] | None = None
    min: float | None = None
    max: float | None = None
    default: str | int | float | None = None
    description: str = ""

    @model_validator(mode="after")
    def _closed_constraint(self) -> ParamSpec:
        if self.type == "enum":
            if not self.values:
                raise ValueError("enum param requires a non-empty 'values' list")
            if self.min is not None or self.max is not None:
                raise ValueError("enum param may not carry numeric bounds")
        else:
            if self.min is None or self.max is None:
                raise ValueError(f"{self.type} param requires BOTH 'min' and 'max'")
            if self.min > self.max:
                raise ValueError("param bounds are inverted (min > max)")
            if self.values is not None:
                raise ValueError(f"{self.type} param may not carry enum 'values'")
            if self.type == "int" and (self.min != int(self.min) or self.max != int(self.max)):
                raise ValueError("int param bounds must be whole numbers")
        if self.default is not None:
            self.check("default", self.default)
        return self

    def check(self, name: str, value: Any) -> Any:
        """Validate ``value`` against the constraint; return it or raise ValueError."""
        if self.type == "enum":
            if not isinstance(value, str) or value not in (self.values or []):
                raise ValueError(f"param '{name}' must be one of {self.values}, got {value!r}")
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"param '{name}' must be a number, got {value!r}")
        if self.type == "int" and not isinstance(value, int):
            raise ValueError(f"param '{name}' must be an integer, got {value!r}")
        if not (self.min <= value <= self.max):  # type: ignore[operator]
            raise ValueError(
                f"param '{name}' must be within [{self.min}, {self.max}], got {value!r}"
            )
        return value

    def representative(self) -> Any:
        """A canonical in-constraint value (default, else first enum value / min).

        Closed constraints make this total: every declarable param has one, so
        a definition can always be previewed without a caller-supplied value.
        """
        if self.default is not None:
            return self.default
        if self.type == "enum":
            return (self.values or [])[0]
        low = self.min if self.min is not None else 0.0
        return int(low) if self.type == "int" else low


class VarChange(BaseModel):
    """One var mutation in an action: set ``cls/name`` dot-``path`` to ``value``."""

    cls: str = Field(
        json_schema_extra={
            "x-dfe-enum-source": {
                "endpoint": "/api/v1/gitops/classes",
                "value_key": "name",
            }
        }
    )
    name: str = Field(
        json_schema_extra={
            "x-dfe-enum-source": {
                "endpoint": "/api/v1/gitops/classes/{cls}/resources",
                "params": {"cls": "cls"},
            }
        }
    )
    path: str = Field(
        json_schema_extra={
            "x-dfe-enum-source": {
                "endpoint": "/api/v1/gitops/classes/{cls}/resources/{name}/vars",
                "params": {"cls": "cls", "name": "name"},
                "value_key": "path",
            }
        }
    )
    value: Any


class ActionDef(BaseModel):
    """A curated, RBAC-gated bundle of var changes (a "big dial").

    Invoking the action applies ALL ``changes`` in ONE commit, gated on
    ``required_action`` (an RBAC string checked at invoke time). Left empty,
    ``required_action`` defaults to ``action:invoke:<name>`` - the convention
    the shipped roles grant on - so it only ever needs hand-writing for a
    deliberately shared or non-standard handle.
    """

    name: str
    description: str = ""
    required_action: str = Field(
        default="",
        description="RBAC string checked at invoke time; empty derives action:invoke:<name>",
    )
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    changes: list[VarChange] = Field(default_factory=list)

    @model_validator(mode="after")
    def _default_required_action(self) -> ActionDef:
        if not self.required_action:
            self.required_action = f"action:invoke:{self.name}"
        return self

    @model_validator(mode="after")
    def _check_param_refs(self) -> ActionDef:
        """Every ``{"$param": ...}`` reference must resolve at DEFINE time.

        Catching a dangling reference, a stray key, a map on a non-enum param,
        or a map that does not cover every enum value here means an invoke can
        never fail on the wiring - only on the caller's supplied values.
        """
        for ch in self.changes:
            ref = param_ref(ch.value)
            if ref is None:
                continue
            pname, mapping = ref
            if set(ch.value) - {"$param", "map"}:
                raise ValueError(
                    f"change {ch.cls}/{ch.name}:{ch.path}: a param reference may "
                    "only carry '$param' and 'map'"
                )
            if not isinstance(pname, str) or pname not in self.params:
                raise ValueError(
                    f"change {ch.cls}/{ch.name}:{ch.path} references undeclared param {pname!r}"
                )
            if mapping is not None:
                spec = self.params[pname]
                if spec.type != "enum":
                    raise ValueError(
                        f"change {ch.cls}/{ch.name}:{ch.path}: 'map' requires an "
                        f"enum param, '{pname}' is {spec.type}"
                    )
                if not isinstance(mapping, dict) or set(mapping) != set(spec.values or []):
                    raise ValueError(
                        f"change {ch.cls}/{ch.name}:{ch.path}: 'map' must cover "
                        f"exactly the enum values {spec.values}"
                    )
        return self


class ProtectedPolicy(BaseModel):
    """Vars locked to default. Patterns match ``cls:name:path`` via fnmatch.

    e.g. ``helmvars:*:replicaCount`` or ``helmvars:receiver-default:config.kafka.*``.
    A protected var can only be changed by a caller holding the override grant, and
    a write at its parent or below it counts as changing it.
    """

    name: str
    description: str = ""
    protected: list[str] = Field(default_factory=list)
