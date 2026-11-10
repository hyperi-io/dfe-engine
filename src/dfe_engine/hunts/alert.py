"""Hunt alert dispatcher -- send notifications when hunts produce results.

Uses Apprise (https://github.com/caronc/apprise) for multi-channel
notification dispatch. Supports Slack, email, PagerDuty, Teams, and 90+
other services via URL-based configuration.

Alert destinations are defined once and referenced by name:

    # Register a destination through the engine API, its URL kept in the secrets backend
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
import json
from pathlib import Path
from typing import Any

import apprise
from pydantic import BaseModel, Field, field_validator
from scalo.expression import (
    evaluate_condition,
)
from scalo.expression import (
    validate as validate_expression,
)
from scalo.logger import logger
from scalo.secrets import SecretsError, SecretsManager

from dfe_engine.names import MAX_NAME_LENGTH, VALID_NAME
from dfe_engine.secrets import MIRROR_PROVIDER, DfeSecrets, mirror_put, mirror_remove
from dfe_engine.yaml_utils import contained_yaml_file, yaml_dump

# -- Destination Registry ------------------------------------------


class AlertDestinationNameError(ValueError):
    """A destination name no secrets backend or destinations directory can hold."""


DESTINATION_NAME_RULE = (
    "a letter or digit, then letters, digits, '.', '_' or '-', "
    f"at most {MAX_NAME_LENGTH} characters"
)
"""What a destination name must be, worded for a refusal."""

DESTINATION_SECRETS_DIR = "alert-destinations"
"""The secrets path each destination is stored under, as ``alert-destinations/<name>``."""


def is_destination_name(name: str) -> bool:
    """Whether *name* can name a destination.

    The name is a secret name on every backend (AWS allows no space, a file no '/') and a
    file stem in the destinations directory, so it takes the store-name rule.

    Args:
        name: The destination name, often straight off a request.

    Returns:
        True when *name* matches :data:`DESTINATION_NAME_RULE`.
    """
    return VALID_NAME.match(name) is not None


def destination_secret_path(name: str) -> str:
    """The secrets path holding destination *name*'s URL and enabled flag."""
    return f"{DESTINATION_SECRETS_DIR}/{name}"


def _secret_value(destination: AlertDestination) -> str:
    """The JSON a destination's secret and mirror key hold: its URL and enabled flag."""
    return json.dumps({"url": destination.url, "enabled": destination.enabled})


def _stored(store: Any, name: str) -> dict[str, Any] | None:
    """The stored document for *name*; a name the store refuses to look up holds nothing."""
    try:
        return store.get(name)
    except ValueError:
        return None


class AlertDestination(BaseModel):
    """A named alert destination (Apprise URL with metadata)."""

    name: str = Field(..., description="Unique destination name (e.g. 'slack-dfe-alerts')")
    url: str = Field(..., description="Apprise notification URL")
    description: str = Field(default="", description="Human-readable description")
    enabled: bool = Field(default=True, description="Enable/disable this destination")
    hunt_name: str | None = Field(
        default=None,
        description="Hunt file stem when this destination is owned by a hunt (set via API)",
    )


class AlertDestinationRegistry:
    """Named alert destinations, each URL kept in the secrets backend.

    A destination's URL and enabled flag are one secret at
    :func:`destination_secret_path`, read by name, so the hunt runner needs the
    secrets backend and nothing of the engine's. With a ``directory`` the engine also
    keeps each destination's description and owning hunt there, one YAML file per
    destination, and that directory is the index :meth:`list` walks. With a ``mirror``
    every write also lands in that Kubernetes Secret, keyed by name, for a runner that
    mounts it in place of reaching the engine's volume. With no ``secrets`` the
    registry holds destinations in memory.

    Usage:
        # Engine API: secrets plus the metadata directory it lists from
        registry = AlertDestinationRegistry(secrets=store, directory="/config/alerts")

        # Hunt runner: reads by name, lists nothing
        registry = AlertDestinationRegistry(secrets=store)

        url = registry.resolve("slack-dfe")

    Raises:
        ValueError: A ``directory`` or ``mirror`` was given with no ``secrets``.
    """

    def __init__(
        self,
        secrets: DfeSecrets | None = None,
        directory: str | None = None,
        mirror: SecretsManager | None = None,
    ) -> None:
        self._secrets = secrets
        self._store = None
        self._mirror = mirror
        self._memory: dict[str, AlertDestination] = {}

        if secrets is None and (directory or mirror is not None):
            raise ValueError("an alert destinations directory or mirror needs a secrets backend")
        if directory and secrets is not None:
            from scalo.config import DirectoryConfigStore

            dir_path = Path(directory)
            dir_path.mkdir(parents=True, exist_ok=True)
            self._store = DirectoryConfigStore(directory=str(dir_path), refresh_interval=30)
            self._store.start()
            # A runner mounting this one directory of a file store needs it before any write.
            secrets.ensure_directory(DESTINATION_SECRETS_DIR)
            logger.info("alert destination metadata directory opened", directory=str(dir_path))

    def add(self, destination: AlertDestination) -> None:
        """Register a destination, overwriting one of the same name.

        The metadata is written before the secret, so a failed secret write leaves a
        destination that reads as absent rather than one no listing shows. The mirror
        is written last, so a refused mirror leaves the backend current for
        :meth:`sync_mirror` to copy.

        Raises:
            AlertDestinationNameError: The name breaks :data:`DESTINATION_NAME_RULE`.
            SecretsError: The secrets backend or the mirror Secret refused the write.
        """
        if not is_destination_name(destination.name):
            raise AlertDestinationNameError(
                f"alert destination name {destination.name!r} must be {DESTINATION_NAME_RULE}"
            )
        if self._secrets is None:
            self._memory[destination.name] = destination
            return
        if self._store is not None:
            yaml_path = contained_yaml_file(self._store._directory, destination.name)
            if yaml_path is None:
                raise AlertDestinationNameError(
                    f"alert destination name {destination.name!r} resolves outside the "
                    "destinations directory"
                )
            metadata = destination.model_dump(
                include={"description", "hunt_name"}, exclude_none=True
            )
            yaml_dump(metadata, yaml_path)
            self._store._refresh_all()
        value = _secret_value(destination)
        self._secrets.put(destination_secret_path(destination.name), value)
        if self._mirror is not None:
            mirror_put(self._mirror, destination.name, value.encode("utf-8"))

    def get(self, name: str) -> AlertDestination:
        """Get a destination by name.

        Raises:
            KeyError: No destination is stored under *name*.
            SecretsError: The stored secret is not a destination.
        """
        if self._secrets is None:
            return self._memory[name]
        if not is_destination_name(name):
            raise KeyError(name)
        stored = self._secrets.get_or_none(destination_secret_path(name))
        if stored is None:
            raise KeyError(name)
        metadata = (_stored(self._store, name) if self._store is not None else None) or {}
        try:
            return AlertDestination.model_validate({**metadata, **json.loads(stored), "name": name})
        except (TypeError, ValueError) as exc:
            raise SecretsError(f"alert destination {name!r} holds no readable destination") from exc

    def resolve(self, name: str) -> str | None:
        """Resolve a destination name to its Apprise URL. Returns None if not found or disabled."""
        try:
            dest = self.get(name)
        except KeyError:
            return None
        if dest.enabled:
            return dest.url
        return None

    def remove(self, name: str) -> bool:
        """Remove a destination by name: the secret, then its metadata, then its mirror key.

        Returns:
            True when the secret, the metadata or the mirror key existed.

        Raises:
            SecretsError: The secrets backend or the mirror Secret refused the removal.
        """
        if self._secrets is None:
            return self._memory.pop(name, None) is not None
        if not is_destination_name(name):
            return False
        path = destination_secret_path(name)
        existed = self._secrets.exists(path)
        self._secrets.delete(path)
        if self._store is not None:
            yaml_path = contained_yaml_file(self._store._directory, name)
            if yaml_path is not None and yaml_path.exists():
                yaml_path.unlink()
                existed = True
            # _refresh_all only picks up new or changed files, so drop the cached entry.
            with self._store._lock:
                self._store._cache.pop(name, None)
        if self._mirror is not None and mirror_remove(self._mirror, name):
            existed = True
        return existed

    def sync_mirror(self) -> builtins.list[str]:
        """Bring the mirror Secret into step with the destinations the directory indexes.

        Writes each destination whose key holds something else and removes every key no
        destination has, so a Secret recreated empty, or a write the mirror refused, is
        caught up with no destination write.

        Returns:
            The keys written or removed, sorted. Empty with no mirror or no directory.

        Raises:
            SecretsError: The secrets backend or the mirror Secret refused.
        """
        if self._mirror is None or self._store is None:
            return []
        changed = []
        wanted = {d.name: _secret_value(d) for d in self.list()}
        for name, value in wanted.items():
            if mirror_put(self._mirror, name, value.encode("utf-8")):
                changed.append(name)
        for key in self._mirror.list_sync(provider=MIRROR_PROVIDER):
            if key not in wanted and mirror_remove(self._mirror, key):
                changed.append(key)
        if changed:
            logger.info("alert destinations brought into step in the mirror Secret", keys=changed)
        return sorted(changed)

    def list(self) -> builtins.list[AlertDestination]:
        """List the destinations the metadata directory indexes, or those held in memory.

        A registry reading by name alone (the hunt runner's) lists none. An index entry
        whose secret is gone is left out. Return annotation is ``builtins.list`` because
        this method named ``list`` shadows the builtin for the type checker.
        """
        if self._secrets is None:
            return list(self._memory.values())
        if self._store is None:
            return []
        results = []
        for name in sorted(self._store.list_tables()):
            try:
                results.append(self.get(name))
            except KeyError:
                logger.warning("alert destination metadata has no stored secret", name=name)
        return results

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

    def close(self) -> None:
        """Stop the metadata directory's background refresh thread, where there is one."""
        if self._store is not None:
            self._store.stop()

    def __len__(self) -> int:
        return len(self.list())

    def __contains__(self, name: str) -> bool:
        try:
            self.get(name)
        except KeyError:
            return False
        return True


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
    # Compared with None: a registry reading by name lists nothing, so its len() is 0.
    if destination_names and destination_registry is not None:
        channels.extend(destination_registry.resolve_many(destination_names))
    elif destination_names:
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
