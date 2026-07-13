#  Project:      dfe-engine
#  File:         sigma/catalog.py
#  Purpose:      id-keyed gitcrud sigma catalogue: upsert/merge, selection, providers
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The sigma rule CRUD catalogue over gitcrud - one id-keyed store for every feed.

Every provider (and the default import file) UPSERTs its normalised rules into ONE
gitcrud-backed store keyed by the sigma `id` UUID, so import is idempotent and
history/survivability come free (spec section G):

  * new id                       -> ADD
  * unchanged (same content)     -> SKIP (no commit)
  * upstream changed, no local edit  -> UPDATE to upstream
  * upstream changed AFTER a local edit -> MERGE, local-edit-wins, drift recorded

The merge REUSES the deep_merge existing-wins pattern already used for the gitops
publish merge (collect_deploy_artifacts): the incoming upstream rule is the base,
the operator's local edits are the override that wins - so a re-import never
clobbers a local edit. Each stored rule carries provenance
{origin, upstream_modified, local_edited, drift, source_ref}.

Three gitcrud classes live in a sigma-local registry (NOT the shared
default_registry, whose class set is asserted elsewhere) over the SAME deploy repo:
  * sigma_rules      - one YAML doc per rule (key = id)
  * sigma_selections - a single 'selected' list doc (the meta-level curation list)
  * sigma_providers  - one YAML doc per provider config
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dfe_engine.gitcrud import GitCrud, ResourceClass, ResourceClassRegistry, ResourceNotFoundError
from dfe_engine.gitcrud.commit_policy import CommitContext, build_message
from dfe_engine.yaml_utils import deep_merge

from .providers import ProviderConfig, ProviderKind, SigmaProvider, SigmaRuleDoc

RULES_CLASS = "sigma_rules"
SELECTIONS_CLASS = "sigma_selections"
PROVIDERS_CLASS = "sigma_providers"
VIEWS_CLASS = "sigma_views"  # one CRUD source-view definition per source (see sigma/views.py)
_SELECTION_DOC = "selected"  # single list doc holding the curated selection


def sigma_registry() -> ResourceClassRegistry:
    """The sigma gitcrud classes, all governed by the ``sigma`` RBAC prefix."""
    return ResourceClassRegistry(
        [
            ResourceClass(RULES_CLASS, "sigma/rules", rbac_prefix="sigma"),
            ResourceClass(SELECTIONS_CLASS, "sigma/selections", rbac_prefix="sigma"),
            ResourceClass(PROVIDERS_CLASS, "sigma/providers", rbac_prefix="sigma"),
            ResourceClass(VIEWS_CLASS, "sigma/views", rbac_prefix="sigma"),
        ]
    )


def _sigma_crud(base: GitCrud) -> GitCrud:
    """A GitCrud over the SAME repo as ``base`` but with the sigma class registry."""
    return GitCrud(base.repo, sigma_registry())


def _msg(scope: str, summary: str, actor: str) -> str:
    """A conforming, attributed commit message (type 'cfg' - sigma maps there)."""
    return build_message(CommitContext(ctype="cfg", scope=scope, summary=summary, actor=actor))


def _same(a: Any, b: Any) -> bool:
    """Order-independent value equality that survives the YAML date round-trip.

    ruamel's safe loader resolves an ISO date scalar (``2023-01-01``) back to a
    datetime.date, while a freshly-normalised doc holds the isoformat STRING. A
    naive ``==`` would then report a phantom change on every re-sync and defeat the
    skip/no-op contract. json.dumps(default=str) coerces BOTH sides to the same
    canonical text, so only a REAL content change compares unequal.
    """
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


# ---- sync report --------------------------------------------------------------


@dataclass
class SyncReport:
    """Outcome of importing a batch of provider docs into the catalogue."""

    source: str
    total: int = 0
    added: int = 0
    updated: int = 0
    skipped: int = 0
    merged: int = 0
    committed: bool = False
    commit_sha: str | None = None
    warnings: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "total": self.total,
            "added": self.added,
            "updated": self.updated,
            "skipped": self.skipped,
            "merged": self.merged,
            "committed": self.committed,
            "commit_sha": self.commit_sha,
            "warnings": self.warnings or [],
        }


# ---- rule catalogue store -----------------------------------------------------


class SigmaCatalogStore:
    """CRUD over the id-keyed sigma rule catalogue (gitcrud sigma_rules)."""

    def __init__(self, crud: GitCrud) -> None:
        self._crud = _sigma_crud(crud)

    # -- reads --

    def list_ids(self) -> list[str]:
        return self._crud.list(RULES_CLASS)

    def get_rule(self, rule_id: str) -> dict[str, Any]:
        """The full stored rule doc ({id, title, rule, provenance}); raises if absent."""
        return self._crud.get(RULES_CLASS, rule_id)

    def exists(self, rule_id: str) -> bool:
        try:
            self._crud.get(RULES_CLASS, rule_id)
            return True
        except ResourceNotFoundError:
            return False

    def summaries(self) -> list[dict[str, Any]]:
        """Lightweight rows for the catalogue list (no full detection payload)."""
        out: list[dict[str, Any]] = []
        for rule_id in self.list_ids():
            try:
                doc = self._crud.get(RULES_CLASS, rule_id)
            except ResourceNotFoundError:
                continue
            prov = doc.get("provenance", {}) or {}
            out.append(
                {
                    "id": doc.get("id", rule_id),
                    "title": doc.get("title", ""),
                    "origin": prov.get("origin", ""),
                    "upstream_modified": _as_text(prov.get("upstream_modified")),
                    "local_edited": bool(prov.get("local_edited", False)),
                    "drift": bool(prov.get("drift", False)),
                }
            )
        return out

    # -- provider import (idempotent upsert) --

    def _stored_from_doc(self, doc: SigmaRuleDoc) -> dict[str, Any]:
        return {
            "id": doc.id,
            "title": doc.title,
            "rule": doc.rule,
            "provenance": {
                "origin": doc.origin,
                "upstream_modified": doc.change_key,
                "local_edited": False,
                "drift": False,
                "source_ref": doc.source_ref,
            },
        }

    def _merge_local(self, existing: dict[str, Any], doc: SigmaRuleDoc) -> dict[str, Any]:
        """Local-edit-wins merge: upstream is the base, the operator's edits override.

        Same existing-wins deep_merge as the gitops publish merge - a shared key
        keeps the operator's value; a brand-new upstream key still lands. Drift is
        recorded when the upstream change signal moved.
        """
        existing_rule = existing.get("rule", {}) or {}
        # replace_lists: the operator's list (tags, references, detection value
        # lists) wins WHOLESALE over upstream. Without it deep_merge APPENDS, so
        # every re-sync duplicates shared list items (and reinstates ones the
        # operator removed), the merged doc never equals the stored one, and every
        # poll commits churn. See P1.3.
        merged_rule = deep_merge(
            copy.deepcopy(doc.rule), copy.deepcopy(existing_rule), replace_lists=True
        )
        prev = existing.get("provenance", {}) or {}
        new_upstream = doc.change_key
        return {
            "id": doc.id,
            # keep the operator's (possibly edited) title, not the upstream one
            "title": existing.get("title", doc.title),
            "rule": merged_rule,
            "provenance": {
                "origin": prev.get("origin", doc.origin),
                "upstream_modified": new_upstream,
                "local_edited": True,
                # STICKY drift (P2.16): once flagged, drift stays set until an
                # operator action clears it (edit_rule / adopt_rule). Recomputing
                # from scratch each sync would self-clear the flag on the very next
                # poll (upstream unchanged -> False), erasing the review signal
                # before an operator ever sees it.
                "drift": bool(prev.get("drift", False))
                or (_as_text(new_upstream) != _as_text(prev.get("upstream_modified"))),
                "source_ref": prev.get("source_ref", doc.source_ref),
            },
        }

    def import_docs(
        self, docs: list[SigmaRuleDoc], actor: str, *, source: str = "", warnings=None
    ) -> SyncReport:
        """Idempotent UPSERT of a batch of normalised docs in ONE commit.

        Only genuinely-changed rules are written (via a single put_many), so a
        re-sync of unchanged rules produces NO commit. Counts are classified before
        the write: add / update / skip / merge (local-edit-preserving).
        """
        existing_names = set(self._crud.list(RULES_CLASS))
        report = SyncReport(source=source or "", warnings=list(warnings or []))
        writes: list[tuple[str, str, dict]] = []
        seen: set[str] = set()

        for doc in docs:
            if doc.id in seen:  # duplicate id inside one fetch - first wins
                continue
            seen.add(doc.id)
            new_stored = self._stored_from_doc(doc)

            if doc.id not in existing_names:
                writes.append((RULES_CLASS, doc.id, new_stored))
                report.added += 1
                continue

            existing = self._crud.get(RULES_CLASS, doc.id)
            local_edited = bool((existing.get("provenance", {}) or {}).get("local_edited", False))
            if not local_edited:
                if _same(existing, new_stored):
                    report.skipped += 1
                else:
                    writes.append((RULES_CLASS, doc.id, new_stored))
                    report.updated += 1
            else:
                merged = self._merge_local(existing, doc)
                if _same(existing, merged):
                    report.skipped += 1
                else:
                    writes.append((RULES_CLASS, doc.id, merged))
                    report.merged += 1

        report.total = len(seen)
        if writes:
            scope = (source or "sigma")[:20]
            result = self._crud.put_many(
                writes, actor, _msg(scope, f"import {len(writes)} rules", actor)
            )
            report.committed = result.changed
            report.commit_sha = result.commit_sha
        return report

    # -- operator edits --

    def edit_rule(
        self, rule_id: str, rule: dict[str, Any], actor: str, *, title: str | None = None
    ) -> dict[str, Any]:
        """Replace a rule's content and mark it locally edited (survives re-imports).

        Setting local_edited=True is what makes a later provider re-import preserve
        this edit instead of overwriting it. Raises ResourceNotFoundError if absent.
        """
        doc = self._crud.get(RULES_CLASS, rule_id)
        doc["rule"] = rule
        if title is not None:
            doc["title"] = title
        prov = doc.setdefault("provenance", {})
        prov["local_edited"] = True
        # The operator has acted on the rule -> the catalogue-drift review signal is
        # resolved (P2.16: sticky drift is cleared only by an operator action).
        prov["drift"] = False
        self._crud.put(RULES_CLASS, rule_id, doc, actor, message=_msg(rule_id[:20], "edit", actor))
        return doc

    def adopt_rule(self, rule_id: str, actor: str) -> dict[str, Any]:
        """Detach a rule from upstream (local_edited=True) without changing content.

        An 'adopted' rule is pinned to its current content - a subsequent provider
        sync records drift but never overwrites it.
        """
        doc = self._crud.get(RULES_CLASS, rule_id)
        prov = doc.setdefault("provenance", {})
        # Adopting resolves any pending catalogue-drift review signal (P2.16). Clear
        # drift even when already local_edited, so an explicit adopt acknowledges it.
        drift_pending = bool(prov.get("drift", False))
        if not prov.get("local_edited", False) or drift_pending:
            prov["local_edited"] = True
            prov["drift"] = False
            self._crud.put(
                RULES_CLASS, rule_id, doc, actor, message=_msg(rule_id[:20], "adopt", actor)
            )
        return doc

    def delete_rule(self, rule_id: str, actor: str) -> None:
        """Remove a rule from the catalogue (raises if absent)."""
        self._crud.delete(RULES_CLASS, rule_id, actor, message=_msg(rule_id[:20], "delete", actor))


# ---- selection store (the meta-level 'which rules to implement' list) ----------


class SigmaSelectionStore:
    """CRUD over the curated selection list - which catalogued rules to implement."""

    def __init__(self, crud: GitCrud) -> None:
        self._crud = _sigma_crud(crud)

    def list_selected(self) -> list[str]:
        try:
            doc = self._crud.get(SELECTIONS_CLASS, _SELECTION_DOC)
        except ResourceNotFoundError:
            return []
        return list(doc.get("rules", []) or [])

    def is_selected(self, rule_id: str) -> bool:
        return rule_id in set(self.list_selected())

    def _write(self, ids: list[str], actor: str, summary: str) -> None:
        self._crud.put(
            SELECTIONS_CLASS,
            _SELECTION_DOC,
            {"rules": sorted(set(ids))},
            actor,
            message=_msg("selected", summary, actor),
        )

    def select(self, rule_id: str, actor: str) -> bool:
        """Add a rule to the selection. Returns False if it was already selected."""
        ids = self.list_selected()
        if rule_id in ids:
            return False
        ids.append(rule_id)
        self._write(ids, actor, f"select {rule_id[:20]}")
        return True

    def deselect(self, rule_id: str, actor: str) -> bool:
        """Remove a rule from the selection. Returns False if it was not selected."""
        ids = self.list_selected()
        if rule_id not in ids:
            return False
        ids = [i for i in ids if i != rule_id]
        self._write(ids, actor, f"deselect {rule_id[:20]}")
        return True


# ---- provider config store ----------------------------------------------------


def default_provider_configs() -> list[ProviderConfig]:
    """The built-in OOTB providers - SigmaHQ is the default git-repo feed + test case."""
    return [
        ProviderConfig(
            name="sigmahq",
            kind=ProviderKind.GIT_REPO,
            enabled=True,
            poll_interval_seconds=86400,
            options={
                "url": "https://github.com/SigmaHQ/sigma.git",
                "branch": "master",
                "subdir": "rules",
            },
        )
    ]


class SigmaProviderStore:
    """CRUD over provider configs, overlaying the built-in defaults.

    A built-in default (SigmaHQ) is visible OOTB WITHOUT a git write; the first
    mutation (create/update/enable/disable) materialises it into the store. Reads
    are therefore side-effect free.
    """

    def __init__(self, crud: GitCrud) -> None:
        self._crud = _sigma_crud(crud)

    def _stored(self) -> dict[str, ProviderConfig]:
        out: dict[str, ProviderConfig] = {}
        for name in self._crud.list(PROVIDERS_CLASS):
            try:
                out[name] = ProviderConfig.model_validate(self._crud.get(PROVIDERS_CLASS, name))
            except ResourceNotFoundError:
                continue
        return out

    def list_configs(self) -> list[ProviderConfig]:
        merged = {c.name: c for c in default_provider_configs()}
        merged.update(self._stored())  # a stored override wins over the builtin default
        return [merged[name] for name in sorted(merged)]

    def get_config(self, name: str) -> ProviderConfig:
        stored = self._stored().get(name)
        if stored is not None:
            return stored
        for builtin in default_provider_configs():
            if builtin.name == name:
                return builtin
        raise ResourceNotFoundError(f"{PROVIDERS_CLASS}/{name}")

    def save_config(self, config: ProviderConfig, actor: str) -> ProviderConfig:
        self._crud.put(
            PROVIDERS_CLASS,
            config.name,
            config.model_dump(mode="json"),
            actor,
            message=_msg(config.name[:20], "provider config", actor),
        )
        return config

    def set_enabled(self, name: str, enabled: bool, actor: str) -> ProviderConfig:
        config = self.get_config(name)
        updated = config.model_copy(update={"enabled": enabled})
        return self.save_config(updated, actor)

    def delete_config(self, name: str, actor: str) -> None:
        """Remove a stored provider config (a built-in default reverts to its default)."""
        self._crud.delete(
            PROVIDERS_CLASS, name, actor, message=_msg(name[:20], "delete provider", actor)
        )


# ---- sync orchestration -------------------------------------------------------


async def sync_provider(
    provider: SigmaProvider,
    catalog: SigmaCatalogStore,
    actor: str,
    *,
    since: datetime | None = None,
) -> SyncReport:
    """Fetch from one provider and upsert into the catalogue - the glue for a sync."""
    docs = await provider.fetch(since)
    # import_docs is a SYNC dulwich commit (put_many) - offload it so the sync task
    # never blocks the event loop, matching the offloaded gitcrud API paths (P2.18).
    return await asyncio.to_thread(
        catalog.import_docs, docs, actor, source=provider.name, warnings=provider.last_warnings
    )


def _as_text(value: Any) -> str | None:
    """Coerce a (possibly YAML-date) change signal to a stable string for compare/JSON."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.date().isoformat()
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else str(value)
