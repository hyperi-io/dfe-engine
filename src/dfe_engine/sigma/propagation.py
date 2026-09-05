#  Project:      dfe-engine
#  File:         sigma/propagation.py
#  Purpose:      Propagate SELECTED sigma rules -> sigma-bound DFE rules + hunts
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Propagate SELECTED sigma catalogue rules into sigma-bound DFE rules and hunts.

This is the last stage of the sigma pipeline. The earlier stages produced:
  * a curated CATALOGUE of normalised sigma rules (sigma/catalog.py) plus a
    SELECTION list of the rule ids an operator chose to implement, and
  * a per-source ``{source}_sigma`` VIEW (sigma/views.py) whose columns are
    aliased to Sigma field names.

For each selected rule this GENERATES a DFE detection Rule per matching source:

  sigma rule detection  --SqlBackend-->  ClickHouse WHERE condition
        over the source's ``{source}_sigma`` view (so the condition references
        Sigma field names DIRECTLY - the view aliases them, hence NO field-map is
        handed to the backend), carried on a Rule with a back-reference to the
        sigma ``id`` (``sigma_rule_id``) + a provenance marker, stored via the
        existing RuleRegistry. The generated rule is then bound into a per-source
        hunt (HuntConfigRegistry) so it actually runs.

It is a GENERATOR feeding the EXISTING rule/hunt stores - it never reimplements
their CRUD. Re-running is idempotent: a binding's id is deterministic per
(sigma id, source), so a re-propagate updates in place.

DRIFT is honoured. A binding is NOT silently regenerated over when either
  * the SIGMA catalogue rule is locally edited / has drift
    (``provenance.local_edited`` / ``provenance.drift``), or
  * the generated BINDING itself was hand-edited since generation (the stored
    WHERE-clause hash no longer matches),
unless an explicit ``force`` is passed. Such bindings are reported as
``skipped_drifted`` rather than clobbered. A brand-new binding (nothing to
clobber) is always generated.

Which SOURCE does a sigma rule bind to? Its ``logsource`` (product/category/
service) is matched to DFE sources via ``SigmaSourceMapper`` - a rule generates
one binding per matching source.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from scalo.logger import logger
from sigma.collection import SigmaCollection

from dfe_engine.gitcrud import ResourceNotFoundError
from dfe_engine.gitcrud.routing import ReviewRouting, WriteOutcome
from dfe_engine.hunts.hunt_config_registry import (
    HuntConfigNotFoundError,
    HuntConfigRegistry,
    default_display_name,
)
from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleNotFoundError, RuleRegistry
from dfe_engine.sigma.catalog import SigmaCatalogStore, SigmaSelectionStore
from dfe_engine.sigma.sigma_backend_clickhouse import SqlBackend
from dfe_engine.sigma.source_mapper import SigmaSourceMapper

# Prefixes for the deterministic generated-artifact names. A binding rule keys on
# (source, sigma id) so a rule that matches N sources yields N distinct bindings;
# a hunt keys on source so all of a source's bindings share ONE per-source hunt.
BINDING_RULE_PREFIX = "sigma"
SIGMA_HUNT_PREFIX = "sigma_hunt"

# Sigma severity levels -> the Rule severity vocabulary (low/medium/high/critical).
# Sigma additionally has 'informational', which maps to the lowest DFE severity.
_SEVERITY_MAP = {
    "informational": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "critical": "critical",
}


def binding_rule_id(sigma_id: str, source: str) -> str:
    """Deterministic Rule id for the (sigma rule, source) binding.

    Hyphens are stripped from the UUID so the id is a clean
    ``sigma_<source>_<hex>`` matching the rules API name pattern
    (``[a-zA-Z0-9_-]+``), and re-propagation targets the SAME id (update, not
    duplicate).
    """
    return f"{BINDING_RULE_PREFIX}_{source}_{sigma_id.replace('-', '')}"


def sigma_hunt_name(source: str) -> str:
    """Deterministic per-source hunt name that runs a source's sigma bindings."""
    return f"{SIGMA_HUNT_PREFIX}_{source}"


def _where_hash(where_clause: str) -> str:
    """Short stable hash of a generated WHERE clause (binding hand-edit detector)."""
    return hashlib.sha256(where_clause.encode("utf-8")).hexdigest()[:16]


def convert_detection_to_where(rule_dict: dict[str, Any]) -> str:
    """Convert a sigma rule dict to a ClickHouse WHERE condition over the sigma view.

    No field mapping is applied: the ``{source}_sigma`` view already aliases each
    column to its Sigma field name, so the backend's verbatim field output (e.g.
    ``EventID = '1'``) lines up with the view's columns. Uses the ``default``
    output format (a plain condition, not the INSERT-INTO alert form). A rule that
    yields several queries is OR-combined. Raises on an unconvertible detection
    (unsupported operator, correlation rule, ...); the caller records the failure
    and moves on.
    """
    collection = SigmaCollection.from_dicts([rule_dict])
    backend = SqlBackend()
    queries = backend.convert(collection, output_format="default")
    conditions = [str(q).strip() for q in queries if str(q).strip()]
    if not conditions:
        raise ValueError("sigma rule produced no detection query")
    if len(conditions) == 1:
        return conditions[0]
    return " OR ".join(f"({c})" for c in conditions)


def sigma_level(rule_dict: dict[str, Any]) -> str:
    """DFE severity derived from the sigma rule ``level`` (default medium)."""
    level = str(rule_dict.get("level", "medium") or "medium").lower()
    return _SEVERITY_MAP.get(level, "medium")


def _catalogue_drifted(doc: dict[str, Any]) -> bool:
    """True when the SIGMA catalogue rule is locally edited or carries drift."""
    prov = doc.get("provenance", {}) or {}
    return bool(prov.get("local_edited", False)) or bool(prov.get("drift", False))


def binding_hand_edited(rule: Rule) -> bool:
    """True when a stored binding's WHERE clause was changed since generation.

    Compares the live WHERE clause against the hash captured at generation. A
    binding with no captured hash (not generated by this propagator) is treated as
    not-edited - there is nothing to protect.
    """
    prov = rule.sigma_provenance or {}
    stored = prov.get("generated_hash")
    if not stored:
        return False
    return _where_hash(rule.where_clause) != stored


def build_binding_rule(sigma_id: str, doc: dict[str, Any], source: str, where_clause: str) -> Rule:
    """Build the sigma-bound DFE Rule (targets the ``{source}_sigma`` view).

    The rule references the view (``source_table = {source}_sigma``) so the WHERE
    clause's Sigma field names resolve to the view's aliased columns. It carries
    the ``sigma_rule_id`` back-reference and a ``sigma_provenance`` marker snapshot
    of the catalogue state + a WHERE-clause hash for later hand-edit detection.
    """
    rule_dict = doc.get("rule", {}) or {}
    prov = doc.get("provenance", {}) or {}
    title = doc.get("title") or rule_dict.get("title") or sigma_id
    return Rule(
        rule_id=binding_rule_id(sigma_id, source),
        name=f"[sigma] {title}",
        severity=sigma_level(rule_dict),
        source=source,
        source_table=sigma_view_name(source),
        where_clause=where_clause,
        original_sql="",
        hunt_name=sigma_hunt_name(source),
        sigma_rule_id=sigma_id,
        sigma_provenance={
            "source": source,
            "sigma_rule_id": sigma_id,
            "upstream_modified": _as_text(prov.get("upstream_modified")),
            "sigma_local_edited": bool(prov.get("local_edited", False)),
            "sigma_drift": bool(prov.get("drift", False)),
            "generated_hash": _where_hash(where_clause),
        },
    )


def sigma_view_name(source: str) -> str:
    """The generated sigma view table name for a source (see sigma/views.py)."""
    return f"{source}_sigma"


def _as_text(value: Any) -> str | None:
    """Coerce a (possibly YAML-date) change signal to a stable string.

    Matches ``sigma.catalog._as_text`` (a ``datetime`` collapses to its date) so a
    stored change signal and its regenerated compare value never spuriously differ
    - a datetime isoformat vs a date isoformat would otherwise read as false drift.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.date().isoformat()
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else str(value)


# -- Report --------------------------------------------------


@dataclass
class PropagationReport:
    """Outcome of a propagate run over the current selection."""

    total_selected: int = 0
    created: list[str] = field(default_factory=list)  # binding rule ids created
    updated: list[str] = field(default_factory=list)  # binding rule ids regenerated
    skipped_drifted: list[dict[str, Any]] = field(default_factory=list)
    skipped_no_source: list[str] = field(default_factory=list)  # sigma ids, no source
    failed: list[dict[str, Any]] = field(default_factory=list)  # {sigma_rule_id, error}
    hunts_touched: list[str] = field(default_factory=list)
    # Bindings whose sigma rule is no longer selected - a deselect left them behind
    # and they keep firing until pruned. Surfaced so the operator sees them.
    stale_bindings: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # In production+team every generated rule and hunt is committed to its own
    # review branch, so a report listing them as created/updated without this says
    # the hunt runner has changes it will not see until each branch is merged.
    review_required: bool = False
    review_branches: list[str] = field(default_factory=list)
    pr_urls: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_selected": self.total_selected,
            "created": list(self.created),
            "updated": list(self.updated),
            "skipped_drifted": list(self.skipped_drifted),
            "skipped_no_source": list(self.skipped_no_source),
            "failed": list(self.failed),
            "hunts_touched": list(self.hunts_touched),
            "stale_bindings": list(self.stale_bindings),
            "warnings": list(self.warnings),
            "review_required": self.review_required,
            "review_branches": list(self.review_branches),
            "pr_urls": list(self.pr_urls),
        }

    def apply_routing(self, routing: ReviewRouting) -> None:
        """Copy a run's folded write-routing onto the report."""
        self.review_required = routing.review_required
        self.review_branches = list(routing.branches)
        self.pr_urls = list(routing.pr_urls)


# -- Propagator ----------------------------------------------


class SigmaPropagator:
    """Generate sigma-bound DFE rules + hunts from the current sigma selection.

    Bridges the sigma catalogue (gitcrud) to the existing rule + hunt registries
    (DirectoryConfigStore). It reads the selection + catalogue + source mappings
    and WRITES through the existing ``RuleRegistry`` / ``HuntConfigRegistry`` - it
    is a generator, not a second CRUD implementation.
    """

    def __init__(
        self,
        *,
        catalog: SigmaCatalogStore,
        selection: SigmaSelectionStore,
        source_mapper: SigmaSourceMapper,
        rule_registry: RuleRegistry,
        hunt_registry: HuntConfigRegistry,
        actor: str,
    ) -> None:
        self._catalog = catalog
        self._selection = selection
        self._mapper = source_mapper
        self._rules = rule_registry
        self._hunts = hunt_registry
        self._actor = actor

    # -- source binding --

    def _sources_for(self, doc: dict[str, Any]) -> list[str]:
        """Source labels a sigma rule's logsource maps to (via SigmaSourceMapper)."""
        logsource = (doc.get("rule", {}) or {}).get("logsource", {}) or {}
        matches = self._mapper.get_sources_for_logsource(
            product=logsource.get("product") or None,
            category=logsource.get("category") or None,
            service=logsource.get("service") or None,
        )
        # Deduplicate + stable order (a source could match on several axes).
        return sorted({s.source for s in matches})

    # -- rule binding (Task A) --

    def _apply_binding(
        self, sigma_id: str, doc: dict[str, Any], source: str, where: str, *, force: bool
    ) -> tuple[str, WriteOutcome | None]:
        """Create or regenerate one (sigma rule, source) binding.

        Returns ``created`` / ``updated`` / ``skipped_drifted`` and the git routing
        outcome of the write (None when nothing was written, or gitops is off).
        Honours drift: an EXISTING binding is not regenerated when the catalogue
        rule drifted OR the binding was hand-edited, unless ``force``.
        """
        rid = binding_rule_id(sigma_id, source)
        existing: Rule | None = None
        if self._rules.exists(rid):
            try:
                existing = self._rules.get(rid)
            except RuleNotFoundError:
                existing = None

        if existing is not None and not force:
            if _catalogue_drifted(doc) or binding_hand_edited(existing):
                return "skipped_drifted", None

        rule = build_binding_rule(sigma_id, doc, source, where)
        if existing is not None:
            # Preserve the original creation time so a re-propagate of an
            # unchanged rule is a true content no-op (no churn commit).
            rule = rule.model_copy(update={"created_at": existing.created_at})
        outcome = self._rules.save(
            rule, created_by=self._actor, description=f"sigma: propagate {rid}"
        )
        return ("updated" if existing is not None else "created"), outcome

    # -- hunt binding (Task B) --

    def _bind_hunt(
        self,
        source: str,
        rule_ids: list[str],
        *,
        cron: str,
        target_table: str,
        customers: list[str],
    ) -> tuple[str, WriteOutcome | None]:
        """Create or update the per-source hunt so it runs ``rule_ids``.

        Returns the hunt name and the git routing outcome of the write.
        Preserves any existing rules (operator-added or pre-existing sigma
        bindings) and their per-rule YAML overrides - only ABSENT bindings are
        appended, mirroring the PUT /hunts merge contract. A brand-new hunt is
        seeded with sensible defaults the operator can later adjust via PUT.
        """
        hunt_name = sigma_hunt_name(source)
        try:
            existing = self._hunts.get(hunt_name)
        except HuntConfigNotFoundError:
            existing = None

        if existing is None:
            config: dict[str, Any] = {
                "display_name": default_display_name(hunt_name),
                "cron": cron,
                "log_buffer": 60,
                "global_target_table_name": target_table,
                "global_source_table_name": sigma_view_name(source),
                "customers": list(customers),
                "rules": [{"rule_name": rid} for rid in rule_ids],
            }
        else:
            config = dict(existing)
            merged = _merged_rule_entries(existing.get("rules"), rule_ids)
            config["rules"] = merged

        outcome = self._hunts.save(
            hunt_name,
            config,
            created_by=self._actor,
            description=f"sigma: bind hunt {hunt_name}",
        )
        return hunt_name, outcome

    # -- orchestration --

    def propagate(
        self,
        *,
        force: bool = False,
        create_hunts: bool = True,
        hunt_cron: str = "*/15 * * * *",
        hunt_target_table: str = "detection",
        hunt_customers: list[str] | None = None,
    ) -> PropagationReport:
        """Generate/refresh bindings + hunts for every SELECTED sigma rule."""
        customers = list(hunt_customers) if hunt_customers else ["default"]
        selected = self._selection.list_selected()
        report = PropagationReport(total_selected=len(selected))
        # Each generated rule and hunt is its own routed write, so the run's review
        # state is the fold of every one of them.
        routing = ReviewRouting()
        # rule ids to (re)bind into each source's hunt (created/updated/still-present).
        bindings_by_source: dict[str, list[str]] = {}

        for sigma_id in selected:
            try:
                doc = self._catalog.get_rule(sigma_id)
            except ResourceNotFoundError:
                report.warnings.append(f"selected rule {sigma_id} is not in the catalogue")
                continue

            sources = self._sources_for(doc)
            if not sources:
                report.skipped_no_source.append(sigma_id)
                continue

            try:
                where = convert_detection_to_where(doc.get("rule", {}) or {})
            except Exception as exc:  # one unconvertible rule must not sink the run
                logger.warning("sigma propagate: convert failed", sigma_id=sigma_id, error=str(exc))
                report.failed.append({"sigma_rule_id": sigma_id, "error": str(exc)})
                continue

            for source in sources:
                rid = binding_rule_id(sigma_id, source)
                status, outcome = self._apply_binding(sigma_id, doc, source, where, force=force)
                routing.record(outcome)
                if status == "created":
                    report.created.append(rid)
                elif status == "updated":
                    report.updated.append(rid)
                else:  # skipped_drifted
                    report.skipped_drifted.append(
                        {"rule_id": rid, "sigma_rule_id": sigma_id, "source": source}
                    )
                # Present bindings (incl. a drift-skipped one that still exists) are
                # bound into the hunt so it runs the full set for the source.
                bindings_by_source.setdefault(source, []).append(rid)

        if create_hunts:
            for source in sorted(bindings_by_source):
                hunt, outcome = self._bind_hunt(
                    source,
                    bindings_by_source[source],
                    cron=hunt_cron,
                    target_table=hunt_target_table,
                    customers=customers,
                )
                routing.record(outcome)
                report.hunts_touched.append(hunt)

        # Surface bindings left behind by a deselect: an existing sigma binding
        # whose sigma id is not in the current selection keeps firing but is no
        # longer wanted. Report it so the operator can DELETE it.
        selected_set = set(selected)
        for row in self._rules.list_rules():
            sigma_id = row.get("sigma_rule_id")
            if sigma_id and sigma_id not in selected_set:
                report.stale_bindings.append(
                    {
                        "rule_id": row["name"],
                        "sigma_rule_id": sigma_id,
                        "source": row.get("source", ""),
                    }
                )

        report.apply_routing(routing)
        return report

    # -- binding queries (for the API list/get/delete) --

    def list_bindings(self) -> list[dict[str, Any]]:
        """Every sigma-generated binding rule with its live drift state."""
        out: list[dict[str, Any]] = []
        for row in self._rules.list_rules():
            if not row.get("sigma_rule_id"):
                continue
            out.append(self._binding_summary(row["name"]))
        return out

    def get_binding(self, rule_id: str) -> dict[str, Any] | None:
        """One binding summary, or None if the rule is absent / not a sigma binding."""
        try:
            rule = self._rules.get(rule_id)
        except RuleNotFoundError:
            return None
        if not rule.sigma_rule_id:
            return None
        return self._binding_summary(rule_id, rule=rule)

    def _binding_summary(self, rule_id: str, *, rule: Rule | None = None) -> dict[str, Any]:
        """Summary row for a binding, with drift computed against the catalogue.

        ``drift`` is TRUE when the binding was hand-edited since generation, or the
        catalogue rule is now locally edited / has drift, or its upstream change
        signal has moved on since the binding was generated, or the catalogue rule
        has since been deleted (orphaned). This is what the operator reviews before
        a re-propagate would (with ``force``) overwrite the binding.
        """
        if rule is None:
            rule = self._rules.get(rule_id)
        prov = rule.sigma_provenance or {}
        sigma_id = rule.sigma_rule_id or ""
        hand_edited = binding_hand_edited(rule)

        catalogue_drift = False
        orphaned = False
        hunts = self._hunts.hunt_names_referencing_rule(rule_id)
        try:
            doc = self._catalog.get_rule(sigma_id) if sigma_id else None
        except ResourceNotFoundError:
            doc = None
        if sigma_id and doc is None:
            orphaned = True
        elif doc is not None:
            catalogue_drift = _catalogue_drifted(doc)
            live_upstream = _as_text((doc.get("provenance", {}) or {}).get("upstream_modified"))
            if live_upstream != prov.get("upstream_modified"):
                catalogue_drift = True

        # A binding whose sigma rule is no longer selected is STALE - a deselect
        # left it behind and it keeps firing until pruned.
        stale = bool(sigma_id) and not self._selection.is_selected(sigma_id)

        return {
            "rule_id": rule_id,
            "sigma_rule_id": sigma_id,
            "display_name": rule.name,
            "source": rule.source or prov.get("source", ""),
            "source_table": rule.source_table or "",
            "severity": rule.severity,
            "hunts": hunts,
            "hand_edited": hand_edited,
            "orphaned": orphaned,
            "selected": not stale,
            "stale": stale,
            "drift": bool(hand_edited or catalogue_drift or orphaned or stale),
        }

    def delete_binding(self, rule_id: str) -> ReviewRouting | None:
        """Delete a binding rule and unlink it from any hunt.

        Returns None if the rule is absent or is not a sigma binding, else the
        folded routing of the rule delete plus every hunt write the unlink caused.
        A generated per-source hunt that is emptied by the unlink is deleted; any
        other hunt is left with the binding removed.
        """
        try:
            rule = self._rules.get(rule_id)
        except RuleNotFoundError:
            return None
        if not rule.sigma_rule_id:
            return None

        routing = ReviewRouting()
        for hunt_name in self._hunts.hunt_names_referencing_rule(rule_id):
            routing.record(self._unlink_rule_from_hunt(hunt_name, rule_id))

        routing.record(self._rules.delete(rule_id))
        return routing

    def _unlink_rule_from_hunt(self, hunt_name: str, rule_id: str) -> WriteOutcome | None:
        """Remove ``rule_id`` from a hunt; delete the hunt if it is left empty."""
        try:
            config = self._hunts.get(hunt_name)
        except HuntConfigNotFoundError:
            return None
        remaining = [
            entry
            for entry in _rule_entries(config.get("rules"))
            if entry.get("rule_name") != rule_id
        ]
        if not remaining:
            # An empty hunt is invalid (min one rule); drop it entirely.
            return self._hunts.delete(hunt_name)
        config = dict(config)
        config["rules"] = remaining
        return self._hunts.save(hunt_name, config, created_by=self._actor)


# -- hunt rule-list helpers (mirror the PUT /hunts merge contract) --


def _rule_entries(rules: Any) -> list[dict[str, Any]]:
    """Normalise a stored hunt ``rules`` list to dict entries (string -> dict)."""
    if not isinstance(rules, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in rules:
        if isinstance(entry, str):
            out.append({"rule_name": entry})
        elif isinstance(entry, dict) and isinstance(entry.get("rule_name"), str):
            out.append(dict(entry))
    return out


def _merged_rule_entries(stored: Any, add_rule_ids: list[str]) -> list[dict[str, Any]]:
    """Existing entries (overrides preserved) plus any missing binding names."""
    entries = _rule_entries(stored)
    present = {entry["rule_name"] for entry in entries}
    for rid in add_rule_ids:
        if rid not in present:
            entries.append({"rule_name": rid})
            present.add(rid)
    return entries
