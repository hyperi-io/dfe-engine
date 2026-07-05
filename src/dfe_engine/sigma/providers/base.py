#  Project:      dfe-engine
#  File:         sigma/providers/base.py
#  Purpose:      SigmaProvider adapter interface + normalised rule doc + factory
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Pluggable Sigma rule providers - fetch external rules, normalise via pySigma.

A provider FETCHES sigma rules from some external feed (a git repo, the Valhalla
JSON API, a local import directory) and returns NORMALISED SigmaRuleDoc objects:
each parsed + validated by pySigma so it carries a mandatory `id` (UUID) plus the
optional `modified` / `date` change signals, tagged with a provenance `origin`.

The store (dfe_engine.sigma.catalog) UPSERTs those docs by `id` into the id-keyed
gitcrud catalogue - so every provider, and the default import file, feed ONE store
the same way (see docs/superpowers/specs, section G). Providers never touch the
store; the sync orchestrator (catalog.sync_provider) glues fetch -> upsert.

Auth (git token / api key) resolves through the scalo.secrets seam (DfeSecrets):
the provider config holds only a secret PATH, never a secret value.
"""

from __future__ import annotations

import abc
from datetime import date, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from scalo.logger import logger
from sigma.collection import SigmaCollection

if TYPE_CHECKING:
    from dfe_engine.secrets import DfeSecrets


class ProviderKind(str, Enum):
    """The adapter implementation a provider config selects."""

    GIT_REPO = "git_repo"  # clone/pull a git repo of *.yml sigma rules (SigmaHQ + any repo)
    VALHALLA = "valhalla"  # Nextron Valhalla JSON feed
    LOCAL_FILES = "local_files"  # a local directory of *.yml (the default import file)


class AuthKind(str, Enum):
    """How a provider authenticates. Secrets are held via the scalo.secrets seam."""

    NONE = "none"
    API_KEY = "api_key"
    GIT_TOKEN = "git_token"


class ProviderAuth(BaseModel):
    """Auth config for a provider - a secret PATH, resolved via DfeSecrets, never a value."""

    kind: AuthKind = AuthKind.NONE
    secret_path: str = Field(
        default="", description="Path in the scalo.secrets seam holding the token/key"
    )
    username: str = Field(default="", description="Git username for token auth (HTTPS)")


class ProviderConfig(BaseModel):
    """One external sigma provider - the config the operator CRUDs.

    `options` is kind-specific: git_repo -> {url, branch, subdir}; valhalla ->
    {base_url, demo}; local_files -> {directory}.
    """

    name: str = Field(description="Unique provider id (the gitcrud filename key)")
    kind: ProviderKind
    enabled: bool = True
    poll_interval_seconds: int = Field(
        default=3600, ge=0, description="Minimum seconds between polls (rate-limit budget)"
    )
    auth: ProviderAuth = Field(default_factory=ProviderAuth)
    options: dict[str, Any] = Field(default_factory=dict)


class SigmaRuleDoc(BaseModel):
    """A normalised sigma rule fetched from a provider.

    `id` is mandatory (a rule with no UUID cannot be CRUD-keyed and is dropped by
    the normaliser). `rule` is pySigma's canonical dict (logsource + detection +
    metadata). `modified`/`date` are the upstream change signals used for skip /
    incremental decisions. `origin` is the provenance tag.
    """

    id: str = Field(description="Sigma rule UUID (the stable CRUD key)")
    title: str = ""
    rule: dict[str, Any] = Field(description="Normalised sigma rule dict (pySigma to_dict)")
    modified: str | None = Field(default=None, description="Upstream modified date (ISO 8601)")
    date: str | None = Field(default=None, description="Upstream created date (ISO 8601)")
    origin: str = Field(description="Provenance, e.g. 'provider:sigmahq' or 'file'")
    source_ref: str = Field(default="", description="Where in the feed (repo path / rule name)")

    @property
    def change_key(self) -> str | None:
        """The change signal: `modified` if present, else `date`."""
        return self.modified or self.date


def docs_from_collection(
    collection: SigmaCollection, origin: str, source_ref: str = ""
) -> tuple[list[SigmaRuleDoc], list[str]]:
    """Turn a parsed pySigma collection into normalised docs + a list of warnings.

    A rule with no `id` is dropped (it cannot be CRUD-keyed) and recorded as a
    warning, NOT an error - one un-keyable rule must never fail a whole sync.
    """
    docs: list[SigmaRuleDoc] = []
    warnings: list[str] = [str(e) for e in collection.errors]
    for rule in collection.rules:
        rid = getattr(rule, "id", None)
        if rid is None:
            warnings.append(f"skipped rule without id: {getattr(rule, 'title', '?')!r}")
            continue
        modified = getattr(rule, "modified", None)
        created = getattr(rule, "date", None)
        docs.append(
            SigmaRuleDoc(
                id=str(rid),
                title=getattr(rule, "title", "") or "",
                rule=rule.to_dict(),
                modified=modified.isoformat() if modified else None,
                date=created.isoformat() if created else None,
                origin=origin,
                source_ref=source_ref,
            )
        )
    return docs, warnings


def parse_sigma_yaml(
    text: str, origin: str, source_ref: str = ""
) -> tuple[list[SigmaRuleDoc], list[str]]:
    """Parse a sigma YAML document (one or more rules) -> normalised docs + warnings.

    resolve_references=False: each file is parsed on its own, so a correlation rule
    that references a rule in ANOTHER file does not raise here - cross-file
    resolution is a convert-time concern, not an import-time one.
    """
    collection = SigmaCollection.from_yaml(text, collect_errors=True, resolve_references=False)
    return docs_from_collection(collection, origin, source_ref)


def parse_sigma_dicts(
    rules: list[dict[str, Any]], origin: str, source_ref: str = ""
) -> tuple[list[SigmaRuleDoc], list[str]]:
    """Parse already-decoded sigma rule dicts (e.g. Valhalla JSON) -> docs + warnings."""
    collection = SigmaCollection.from_dicts(rules, collect_errors=True, resolve_references=False)
    return docs_from_collection(collection, origin, source_ref)


def modified_since(doc: SigmaRuleDoc, since: datetime | None) -> bool:
    """True if the rule's change date is >= `since` (or `since` is None).

    A rule with no parseable change date is INCLUDED - we cannot prove it is old,
    and the store's upsert skips it anyway when nothing actually changed.
    """
    if since is None:
        return True
    ref = doc.change_key
    if not ref:
        return True
    try:
        return date.fromisoformat(ref) >= since.date()
    except ValueError:
        return True


class SigmaProvider(abc.ABC):
    """Base adapter: fetch normalised sigma docs from one external feed.

    Subclasses implement `fetch`. `origin` is the provenance stamped on every doc
    the provider yields, so the store can show the source and honour local edits.
    """

    def __init__(self, config: ProviderConfig, *, secrets: DfeSecrets | None = None) -> None:
        self.config = config
        self._secrets = secrets
        self.last_warnings: list[str] = []

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def origin(self) -> str:
        """Provenance tag written onto every fetched doc."""
        return f"provider:{self.config.name}"

    def _secret(self) -> str:
        """Resolve the configured auth secret via the scalo.secrets seam, or ''.

        Returns '' when no secret is configured OR no secrets store is wired, so a
        no-auth provider (SigmaHQ public repo, Valhalla demo key) just works.
        """
        auth = self.config.auth
        if auth.kind == AuthKind.NONE or not auth.secret_path:
            return ""
        if self._secrets is None:
            logger.warning(
                "sigma provider has an auth secret but no secrets store is wired",
                provider=self.name,
            )
            return ""
        return self._secrets.get(auth.secret_path)

    @abc.abstractmethod
    async def fetch(self, since: datetime | None = None) -> list[SigmaRuleDoc]:
        """Fetch (optionally only rules changed since `since`) as normalised docs."""
        raise NotImplementedError


def build_provider(
    config: ProviderConfig,
    *,
    secrets: DfeSecrets | None = None,
    work_dir: Any = None,
) -> SigmaProvider:
    """Construct the adapter for a provider config. `work_dir` is the git clone cache."""
    from .git_repo import GitRepoProvider
    from .local_files import LocalFilesProvider
    from .valhalla import ValhallaProvider

    if config.kind == ProviderKind.GIT_REPO:
        return GitRepoProvider(config, secrets=secrets, work_dir=work_dir)
    if config.kind == ProviderKind.VALHALLA:
        return ValhallaProvider(config, secrets=secrets)
    if config.kind == ProviderKind.LOCAL_FILES:
        return LocalFilesProvider(config, secrets=secrets)
    raise ValueError(f"unknown sigma provider kind: {config.kind!r}")
