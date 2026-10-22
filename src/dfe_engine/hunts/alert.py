"""Hunt alert dispatcher -- send notifications when hunts produce results.

Uses Apprise (https://github.com/caronc/apprise) for multi-channel
notification dispatch. Supports Slack, email, PagerDuty, Teams, and 90+
other services via URL-based configuration.

Alert destinations are defined once and referenced by name:

    # Register a destination (control-plane or settings)
    registry.add(AlertDestination(
        name="slack-dfe-alerts",
        url="slack://TokenA/TokenB/TokenC/#alerts",
        description="DFE alerts -> #dfe-alerts",
    ))

    # Hunt YAML references by name -- CEL trigger syntax
    alerts:
      destinations:
        - slack-dfe-alerts
      triggers:
        - when: "result_count > 0"
        - when: "result_count >= 10"
        - when: 'severity == "critical" && result_count > 0'

Triggers use CEL expressions evaluated against {result_count, ...row_fields}:
- "result_count > 0" -- fire when any result rows are returned
- "result_count >= 10" -- fire when result count meets a threshold
- 'severity == "critical"' -- fire when a field in results matches a condition
- 'severity == "critical" && result_count > 0' -- compound conditions
"""

import builtins
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import apprise
from pydantic import BaseModel, Field, field_validator, model_validator
from scalo.expression import (
    evaluate_condition,
)
from scalo.expression import (
    validate as validate_expression,
)
from scalo.logger import logger

from dfe_engine.gitcrud.commit_policy import CommitPolicyError, validate_name
from dfe_engine.http_errors import describe_http_error
from dfe_engine.yaml_utils import contained_yaml_file, yaml_dump

if TYPE_CHECKING:
    from dfe_engine.gitcrud.routing import ReviewRouting
    from dfe_engine.hunts.deploy_repo import DeployRepoStore
    from dfe_engine.secrets import DfeSecrets

# -- Destination Registry ------------------------------------------

ALERT_CLASS = "alert_destinations"
"""The gitcrud resource class alert destinations live in when gitops is on."""

URL_PATH_FIELD = "url_path"
"""The deploy-repo document field naming where the secrets backend holds the URL."""


def scheme_of(url: str) -> str:
    """The scheme of an Apprise URL, ``slack`` of ``slack://...``, empty where it names none."""
    if "://" in url:
        return url.split("://")[0]
    return ""


def url_secret_path(name: str) -> str:
    """A new secrets-backend path for destination *name*'s URL.

    Each URL gets its own path, so a URL change on a review branch never alters the
    URL main sends to before the branch merges.
    """
    return f"alerts/{name}/url-{uuid.uuid4().hex}"


_URL_LEAF = re.compile(r"url-[0-9a-f]{32}")
"""The last segment :func:`url_secret_path` gives a URL."""


def _owns_url_path(name: str, path: object) -> bool:
    """Whether *path* has the shape :func:`url_secret_path` gives destination *name*.

    A document can name any path, and a file backend follows ``..``, so a path only
    counts when it is exactly ``alerts/<name>/url-<hex>``.
    """
    if not isinstance(path, str):
        return False
    try:
        validate_name(name)
    except CommitPolicyError:
        return False
    leaf = path.removeprefix(f"alerts/{name}/")
    return leaf != path and _URL_LEAF.fullmatch(leaf) is not None


def _refuse_url_path(name: str, path: object) -> None:
    """Log a document's URL path that is not the destination's own, by name and path."""
    logger.warning(
        "Alert destination URL path refused: it is not under the destination's own alerts/<name>/",
        destination=name,
        store_path=path,
    )


class AlertDestinationNameError(ValueError):
    """A destination name whose file would sit outside the destinations directory."""


_NAME_REFUSED_CHARACTERS = ("/", "\\", "\x00")
"""A separator would place the file outside the directory, and a NUL cannot name a file."""

DESTINATION_NAME_RULE = "one file name: not empty, not '.' or '..', and no '/', '\\' or NUL"
"""What a new destination name must be, worded for a refusal."""


def is_destination_name(name: str) -> bool:
    """Whether *name* can name a new destination, one file ``<directory>/<name>.yaml``.

    Args:
        name: The destination name, often straight off a request.

    Returns:
        True when *name* is a single file name, per :data:`DESTINATION_NAME_RULE`.
    """
    if not name or name in (".", ".."):
        return False
    return not any(c in name for c in _NAME_REFUSED_CHARACTERS)


def _stored(store: Any, name: str) -> dict[str, Any] | None:
    """The stored document for *name*; a name the store refuses to look up holds nothing."""
    try:
        return store.get(name)
    except ValueError:
        return None


class AlertDestination(BaseModel):
    """A named alert destination (Apprise URL with metadata)."""

    name: str = Field(..., description="Unique destination name (e.g. 'slack-dfe-alerts')")
    url: str | None = Field(
        default=None,
        description=(
            "Apprise notification URL. None on a destination read from the deploy repo, "
            "whose URL only resolve() reads, and on a write that keeps the stored URL"
        ),
    )
    url_scheme: str = Field(default="", description="The URL's scheme, set from the URL")
    description: str = Field(default="", description="Human-readable description")
    enabled: bool = Field(default=True, description="Enable/disable this destination")
    hunt_name: str | None = Field(
        default=None,
        description="Hunt file stem when this destination is owned by a hunt (set via API)",
    )

    @model_validator(mode="after")
    def _scheme_of_the_url(self) -> Self:
        if self.url is not None:
            self.url_scheme = scheme_of(self.url)
        return self


class AlertDestinationRegistry:
    """Registry of named alert destinations.

    With ``deploy_repo`` the destinations are the deploy repo's
    ``alert_destinations`` class, one document per destination, so every engine
    replica reads the same set and every write is one routed commit. The URL is a
    credential, so it lives in ``secrets`` and the document keeps its path in
    :data:`URL_PATH_FIELD` and its scheme. With a ``directory`` they are YAML files
    under a DirectoryConfigStore. With neither the registry is an in-memory dict
    (tests and bootstrap).

    Usage:
        # File-backed
        registry = AlertDestinationRegistry(directory="/config/alert-destinations")

        # In-memory (tests / bootstrap from settings dict)
        registry = AlertDestinationRegistry()
        registry.load_from_dict({"slack-dfe": "slack://..."})

        url = registry.resolve("slack-dfe")
    """

    def __init__(
        self,
        directory: str | None = None,
        *,
        deploy_repo: DeployRepoStore | None = None,
        secrets: DfeSecrets | None = None,
    ) -> None:
        """Bind the registry to one backend.

        Args:
            directory: YAML directory for the DirectoryConfigStore backend. Ignored
                when ``deploy_repo`` is given.
            deploy_repo: The deploy repo's ``alert_destinations`` class.
            secrets: Holds each URL when ``deploy_repo`` is given.

        Raises:
            ValueError: ``deploy_repo`` is given without ``secrets``.
        """
        if deploy_repo is not None and secrets is None:
            raise ValueError("alert destinations in the deploy repo need a secrets backend")
        self._store = None
        self._memory: dict[str, AlertDestination] = {}
        self._deploy = deploy_repo
        self._secrets = secrets

        if deploy_repo is None and directory:
            dir_path = Path(directory)
            dir_path.mkdir(parents=True, exist_ok=True)
            try:
                from scalo.config import DirectoryConfigStore

                self._store = DirectoryConfigStore(
                    directory=str(dir_path),
                    refresh_interval=30,
                )
                self._store.start()
                logger.info(f"AlertDestinationRegistry: DirectoryConfigStore at {dir_path}")
            except Exception as e:
                logger.warning(
                    f"AlertDestinationRegistry: failed to init DirectoryConfigStore ({e}), "
                    "falling back to in-memory"
                )

    def add(
        self,
        destination: AlertDestination,
        *,
        created_by: str | None = None,
        routing: ReviewRouting | None = None,
    ) -> None:
        """Register a destination (overwrites if name exists).

        Persists to the deploy repo or to YAML when a backend is bound.

        Args:
            destination: The destination. A URL of None keeps the stored one.
            created_by: Username/identity, recorded on a deploy-repo commit.
            routing: Collects where a deploy-repo write landed.

        Raises:
            AlertDestinationNameError: The name cannot name a stored destination.
            KeyError: The URL is None and no destination of that name is stored.
            SecretsError: The secrets backend refused a deploy-repo destination's URL,
                and nothing was committed.
        """
        if self._deploy is not None:
            self._put_in_deploy_repo(
                self._deploy, destination, actor=created_by or "engine", routing=routing
            )
            return
        if destination.url is None:
            kept = self.get(destination.name).url
            destination = AlertDestination.model_validate({**destination.model_dump(), "url": kept})
        # The file name is the key and the URL gives the scheme, so only the rest is stored.
        data = destination.model_dump(exclude={"name", "url_scheme"}, exclude_none=True)
        if self._store is not None:
            yaml_path = contained_yaml_file(self._store._directory, destination.name)
            if yaml_path is None:
                raise AlertDestinationNameError(
                    f"alert destination name {destination.name!r} resolves outside the "
                    "destinations directory"
                )
            yaml_dump(data, yaml_path)
            self._store._refresh_all()
        else:
            self._memory[destination.name] = destination

    @property
    def _urls(self) -> DfeSecrets:
        """The secrets backend holding deploy-repo URLs, bound with the deploy repo."""
        if self._secrets is None:
            raise ValueError("alert destinations in the deploy repo need a secrets backend")
        return self._secrets

    def _put_in_deploy_repo(
        self,
        deploy: DeployRepoStore,
        destination: AlertDestination,
        *,
        actor: str,
        routing: ReviewRouting | None,
    ) -> None:
        """Store a new URL in the secrets backend, then commit the document naming its path.

        A URL the commit replaces is deleted once the commit is on main. On a review
        branch both stay, because main sends to the old one until the branch merges.
        A failed commit leaves its new URL unread rather than risk deleting one a
        pushed branch names.
        """
        name = destination.name
        try:
            validate_name(name)
        except CommitPolicyError as exc:
            raise AlertDestinationNameError(
                f"alert destination name {name!r} cannot name a deploy-repo document: use "
                "letters, digits, '.', '_' and '-' only"
            ) from exc
        stored = deploy.get(name) or {}
        old_path = stored.get(URL_PATH_FIELD, "")
        if destination.url is None:
            if not old_path:
                raise KeyError(name)
            path, scheme = old_path, stored.get("url_scheme", "")
        else:
            path, scheme = url_secret_path(name), destination.url_scheme
            self._urls.put(path, destination.url)
        doc = {
            URL_PATH_FIELD: path,
            "url_scheme": scheme,
            # An unowned destination stores no hunt_name.
            **destination.model_dump(
                include={"description", "enabled", "hunt_name"}, exclude_none=True
            ),
        }
        try:
            outcome = deploy.put(name, doc, actor=actor)
        except Exception:
            if path != old_path:
                logger.warning(
                    "Alert destination URL left in the secrets backend: its commit failed",
                    destination=name,
                    store_path=path,
                )
            raise
        if routing is not None:
            routing.record(outcome)
        if old_path and old_path != path:
            self._retire_url(name, old_path, kept=outcome.review_required)

    def _retire_url(self, name: str, path: str, *, kept: bool) -> None:
        """Delete a URL no document on main names, or log where it stays while main still does.

        A path outside the destination's own is refused, never deleted. A delete the
        backend fails is logged by its path and left for an operator.
        """
        if not _owns_url_path(name, path):
            _refuse_url_path(name, path)
            return
        if kept:
            logger.info(
                "Alert destination URL kept until its review branch merges",
                destination=name,
                store_path=path,
            )
            return
        urls = self._urls
        try:
            urls.delete(path)
        except Exception as exc:
            logger.warning(
                "Alert destination URL left in the secrets backend: no destination reads it",
                destination=name,
                store_path=path,
                **describe_http_error(exc),
            )

    def _stored_doc(self, name: str) -> dict[str, Any] | None:
        """The stored document for *name*, None when absent or not nameable by the backend."""
        if self._deploy is not None:
            return self._deploy.get(name)
        if self._store is not None:
            return _stored(self._store, name)
        return None

    def _destination(self, name: str, data: dict[str, Any]) -> AlertDestination:
        """A stored document as a destination, with no URL when it comes from the deploy repo."""
        # The name is the document's key, never a field inside it.
        fields = {**data, "name": name}
        if self._deploy is not None:
            fields["url"] = None
        return AlertDestination.model_validate(fields)

    def get(self, name: str) -> AlertDestination:
        """Get a destination by name. Raises KeyError if not found.

        A deploy-repo destination comes back with no URL: :meth:`resolve` reads it.
        """
        if self._deploy is None and self._store is None:
            return self._memory[name]
        data = self._stored_doc(name)
        if data is None:
            raise KeyError(name)
        return self._destination(name, data)

    def resolve(self, name: str) -> str | None:
        """Resolve a destination name to its Apprise URL. Returns None if not found or disabled.

        A deploy-repo destination's URL is read from the secrets backend at the path its
        document names. One missing there resolves to None and is logged, and so does a
        path outside the destination's own ``alerts/<name>/``, which is never read.

        Raises:
            SecretsError: The secrets backend did not answer or refused the read.
        """
        if self._deploy is None:
            try:
                dest = self.get(name)
            except KeyError:
                return None
            return dest.url if dest.enabled else None
        data = self._deploy.get(name)
        if data is None or not self._destination(name, data).enabled:
            return None
        path = data.get(URL_PATH_FIELD, "")
        if path and not _owns_url_path(name, path):
            _refuse_url_path(name, path)
            return None
        url = self._urls.get_or_none(path) if path else None
        if url is None:
            logger.warning(
                "Alert destination URL missing from the secrets backend",
                destination=name,
                store_path=path,
            )
        return url

    def remove(
        self,
        name: str,
        *,
        created_by: str | None = None,
        routing: ReviewRouting | None = None,
    ) -> bool:
        """Remove a destination by name. Returns True if it existed.

        A deploy-repo destination's URL is deleted from the secrets backend once the
        document's removal is on main, so no document on main names a missing URL.

        Args:
            name: The destination name.
            created_by: Username/identity, recorded on a deploy-repo commit.
            routing: Collects where a deploy-repo write landed.
        """
        if self._deploy is not None:
            path = (self._deploy.get(name) or {}).get(URL_PATH_FIELD, "")
            outcome = self._deploy.delete(name, actor=created_by or "engine")
            if outcome is None:
                return False
            if routing is not None:
                routing.record(outcome)
            if path:
                self._retire_url(name, path, kept=outcome.review_required)
            return True
        if self._store is not None:
            yaml_path = contained_yaml_file(self._store._directory, name)
            if yaml_path is not None and yaml_path.exists():
                yaml_path.unlink()
                # Clear stale cache entry (_refresh_all only picks up new/changed files)
                with self._store._lock:
                    self._store._cache.pop(name, None)
                return True
            return False
        return self._memory.pop(name, None) is not None

    def list(self) -> builtins.list[AlertDestination]:
        """List all registered destinations.

        Return annotation is ``builtins.list`` because this method named ``list``
        shadows the builtin for the type checker's annotation resolution.
        """
        if self._deploy is None and self._store is None:
            return list(self._memory.values())
        results = []
        for table_name in self._names():
            data = self._stored_doc(table_name)
            if data:
                results.append(self._destination(table_name, data))
        return results

    def _names(self) -> builtins.list[str]:
        """Every stored destination name."""
        if self._deploy is not None:
            return self._deploy.names()
        if self._store is not None:
            return list(self._store.list_tables())
        return list(self._memory)

    def resolve_many(self, names: builtins.list[str]) -> builtins.list[str]:
        """Resolve a list of destination names to Apprise URLs.

        Skips unknown or disabled destinations with a warning.
        """
        urls: list[str] = []
        for name in names:
            url = self.resolve(name)
            if url:
                urls.append(url)
            else:
                logger.warning(f"Alert destination '{name}' not found or disabled, skipping")
        return urls

    def load_from_dict(self, destinations: dict[str, str]) -> None:
        """Bulk-load destinations from a {name: url} dict (e.g. from settings)."""
        for name, url in destinations.items():
            self.add(AlertDestination(name=name, url=url))

    def close(self) -> None:
        """Stop the backing store's background refresh thread (no-op in-memory)."""
        if self._store is not None:
            self._store.stop()

    def __len__(self) -> int:
        return len(self._names())

    def __contains__(self, name: str) -> bool:
        if self._deploy is None and self._store is None:
            return name in self._memory
        return self._stored_doc(name) is not None


# -- Trigger Models -----------------------------------------------


class AlertTrigger(BaseModel):
    """A CEL condition that triggers an alert.

    Examples::

        AlertTrigger(when="result_count > 0")
        AlertTrigger(when="result_count >= 10")
        AlertTrigger(when='severity == "critical" && result_count > 0')
    """

    when: str = Field(..., description="CEL condition expression")

    @field_validator("when")
    @classmethod
    def _validate_when(cls, v: str) -> str:
        errors = validate_expression(v)
        if errors:
            raise ValueError(f"Invalid alert trigger expression: {'; '.join(errors)}")
        return v


# --- DEFERRED (unwired) below: the dispatch half of alerting ---
# AlertConfig / AlertDispatcher / build_alert_config have no src caller today (the
# registry half above -- AlertDestination / AlertDestinationRegistry / AlertTrigger --
# IS live via the API + alerts router). Part of the alerting track (auto-memory
# project_alerting); NOT dead code. It converges with the alerting/ package's Dispatcher
# when the alerting build lands (ONE dispatch seam chosen then). Retained deliberately.


class AlertConfig(BaseModel):
    """Alert configuration for a hunt or globally."""

    channels: list[str] = Field(default_factory=list, description="Apprise notification URLs")
    triggers: list[AlertTrigger] = Field(
        default_factory=list,
        description="Conditions that trigger alerts",
    )
    title_template: str = Field(
        default="DFE Hunt Alert: {hunt_name}",
        description="Notification title template",
    )
    body_template: str = Field(
        default=(
            "Hunt **{hunt_name}** detected **{result_count}** result(s) "
            "for customer **{customer}** (rule: {rule_name})."
        ),
        description="Notification body template (Markdown)",
    )
    enabled: bool = Field(default=True, description="Enable/disable alerts")


class _SafeFormatDict(dict):
    """Dict that returns '{key}' for missing keys during str.format_map().

    Allows body/title templates to contain optional placeholders like
    {match_count} or {group_fields} that are only present for grouped
    alerts without raising KeyError for ungrouped alerts.
    """

    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"


# -- Dispatcher ----------------------------------------------------


class AlertDispatcher:
    """Evaluates triggers and dispatches alerts via Apprise.

    Usage:
        dispatcher = AlertDispatcher(alert_config)
        dispatcher.evaluate_and_send(hunt_context)
    """

    def __init__(self, config: AlertConfig):
        self._config = config
        self._apprise: apprise.Apprise | None = None

    def _get_apprise(self) -> apprise.Apprise:
        """Lazy-init Apprise instance with configured channels."""
        if self._apprise is None:
            self._apprise = apprise.Apprise()
            for url in self._config.channels:
                self._apprise.add(url)
        return self._apprise

    def evaluate_and_send(
        self,
        hunt_name: str,
        customer: str,
        rule_name: str,
        result_count: int,
        results: list[dict[str, Any]] | None = None,
        group_context: dict[str, Any] | None = None,
        score: int = 50,
    ) -> bool:
        """Evaluate triggers against hunt results and send alerts if matched.

        Args:
            hunt_name: Name of the hunt.
            customer: Customer/org that was scanned.
            rule_name: Name of the rule that produced results.
            result_count: Number of result rows.
            results: Optional list of result dicts for field_value triggers.
            group_context: Optional dict with grouped alert details:
                group_fields (dict), match_count (int),
                first_seen (str), last_seen (str).
            score: The hunt's score, passed to the alert template context.

        Returns:
            True if an alert was sent, False otherwise.
        """
        if not self._config.enabled or not self._config.channels:
            return False

        if not self._config.triggers:
            return False

        if not self._should_fire(result_count, results):
            return False

        context = {
            "hunt_name": hunt_name,
            "customer": customer,
            "rule_name": rule_name,
            "result_count": result_count,
            "score": score,
        }

        # Enrich with group context if available
        if group_context:
            context["match_count"] = group_context.get("match_count", result_count)
            context["first_seen"] = group_context.get("first_seen", "")
            context["last_seen"] = group_context.get("last_seen", "")
            group_fields = group_context.get("group_fields", {})
            context["group_fields"] = ", ".join(f"{k}={v}" for k, v in group_fields.items())

        title = self._config.title_template.format_map(_SafeFormatDict(context))
        body = self._config.body_template.format_map(_SafeFormatDict(context))

        return self._send(title, body)

    def _should_fire(self, result_count: int, results: list[dict[str, Any]] | None) -> bool:
        """Check if any trigger condition is met.

        Each trigger's ``when`` expression is evaluated via CEL against a
        context that always includes ``result_count``.  When ``results``
        are provided, each row is merged into the context so per-row
        field conditions (e.g. ``severity == "critical"``) work naturally.
        """
        base_context = {"result_count": result_count}

        for trigger in self._config.triggers:
            if results:
                # Per-row evaluation: merge each row with aggregate context
                for row in results:
                    ctx = {**base_context, **row}
                    if evaluate_condition(trigger.when, ctx):
                        return True
            else:
                # Aggregate-only evaluation
                if evaluate_condition(trigger.when, base_context):
                    return True

        return False

    def _send(self, title: str, body: str) -> bool:
        """Send notification via Apprise. Returns True on success."""
        try:
            ap = self._get_apprise()
            result = ap.notify(title=title, body=body)
            if result:
                logger.info(f"Alert sent: {title}")
            else:
                logger.warning(f"Alert dispatch failed: {title}")
            return bool(result)
        except Exception as e:
            logger.error(f"Alert dispatch error: {e}")
            return False


# -- Factory -------------------------------------------------------


def build_alert_config(
    hunt_data: dict,
    global_channels: list[str] | None = None,
    destination_registry: AlertDestinationRegistry | None = None,
) -> AlertConfig | None:
    """Build AlertConfig from hunt YAML data, resolving named destinations.

    Hunt YAML format::

        alerts:
          destinations:          # named refs -> resolved via registry
            - slack-dfe-alerts
            - pagerduty-oncall
          channels:              # raw Apprise URLs
            - mailto://...
          triggers:
            - when: "result_count > 0"
            - when: 'severity == "critical" && result_count > 0'

    Resolution order:
    1. Resolve destination names via registry -> Apprise URLs
    2. Append any raw channel URLs from hunt YAML
    3. Append global_channels (deduplicated)

    Returns None if no channels resolve.
    """
    alerts_section = hunt_data.get("alerts", {})
    if not isinstance(alerts_section, dict):
        return None

    channels: list[str] = []

    # Resolve named destinations via registry
    destination_names = alerts_section.get("destinations", [])
    if destination_names and destination_registry:
        channels.extend(destination_registry.resolve_many(destination_names))
    elif destination_names and not destination_registry:
        logger.warning(f"Hunt references destinations {destination_names} but no registry provided")

    # Raw channel URLs (backward compat)
    raw_channels = alerts_section.get("channels", [])
    seen = set(channels)
    for ch in raw_channels:
        if ch not in seen:
            channels.append(ch)
            seen.add(ch)

    # Merge global channels (deduplicated)
    if global_channels:
        for ch in global_channels:
            if ch not in seen:
                channels.append(ch)
                seen.add(ch)

    if not channels:
        return None

    triggers_raw = alerts_section.get("triggers", [])
    triggers = [AlertTrigger.model_validate(t) for t in triggers_raw] if triggers_raw else []

    # Default: fire when any results are returned
    if not triggers:
        triggers = [AlertTrigger(when="result_count > 0")]

    return AlertConfig(
        channels=channels,
        triggers=triggers,
        title_template=alerts_section.get(
            "title_template", AlertConfig.model_fields["title_template"].default
        ),
        body_template=alerts_section.get(
            "body_template", AlertConfig.model_fields["body_template"].default
        ),
        enabled=alerts_section.get("enabled", True),
    )
