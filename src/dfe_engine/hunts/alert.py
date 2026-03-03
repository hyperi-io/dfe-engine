"""Hunt alert dispatcher — send notifications when hunts produce results.

Uses Apprise (https://github.com/caronc/apprise) for multi-channel
notification dispatch. Supports Slack, email, PagerDuty, Teams, and 90+
other services via URL-based configuration.

Alert destinations are defined once and referenced by name:

    # Register a destination (control-plane or settings)
    registry.add(AlertDestination(
        name="slack-dfe-alerts",
        url="slack://TokenA/TokenB/TokenC/#alerts",
        description="DFE alerts → #dfe-alerts",
    ))

    # Hunt YAML references by name
    alerts:
      destinations:
        - slack-dfe-alerts
      triggers:
        - type: any_match
        - type: result_count
          operator: ">="
          value: 10

Trigger types:
- any_match: fire when any result rows are returned
- result_count: fire when result count meets a threshold (>=, >, ==)
- field_value: fire when a specific field in results matches a condition
"""

from __future__ import annotations

import operator
from pathlib import Path
from typing import Any, Optional

import apprise
from pydantic import BaseModel, Field

from hyperi_pylib.logger import logger


# ── Destination Registry ──────────────────────────────────────────


class AlertDestination(BaseModel):
    """A named alert destination (Apprise URL with metadata)."""

    name: str = Field(..., description="Unique destination name (e.g. 'slack-dfe-alerts')")
    url: str = Field(..., description="Apprise notification URL")
    description: str = Field(default="", description="Human-readable description")
    enabled: bool = Field(default=True, description="Enable/disable this destination")


class AlertDestinationRegistry:
    """Registry of named alert destinations backed by DirectoryConfigStore.

    When a ``directory`` is provided, destinations are persisted as YAML files
    (one per destination) and the store handles caching, background refresh,
    and optional git-aware writes.  Without a directory the registry operates
    as an in-memory dict (useful for tests and bootstrap).

    Usage:
        # File-backed (production)
        registry = AlertDestinationRegistry(directory="/config/alert-destinations")

        # In-memory (tests / bootstrap from settings dict)
        registry = AlertDestinationRegistry()
        registry.load_from_dict({"slack-dfe": "slack://..."})

        url = registry.resolve("slack-dfe")
    """

    def __init__(self, directory: Optional[str] = None) -> None:
        self._store = None
        self._memory: dict[str, AlertDestination] = {}

        if directory:
            dir_path = Path(directory)
            dir_path.mkdir(parents=True, exist_ok=True)
            try:
                from hyperi_pylib.config import DirectoryConfigStore

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

    def add(self, destination: AlertDestination) -> None:
        """Register a destination (overwrites if name exists).

        Persists to YAML when backed by DirectoryConfigStore.
        """
        if self._store is not None:
            from dfe_engine.yaml_utils import yaml_dump

            # Store url/description/enabled — name comes from the filename
            data = destination.model_dump(exclude={"name"})
            yaml_path = Path(self._store._directory) / f"{destination.name}.yaml"
            yaml_dump(data, yaml_path)
            self._store._refresh_all()
        else:
            self._memory[destination.name] = destination

    def get(self, name: str) -> AlertDestination:
        """Get a destination by name. Raises KeyError if not found."""
        if self._store is not None:
            data = self._store.get(name)
            if data is None:
                raise KeyError(name)
            # Inject name from table key (filename) — not stored inside YAML
            data["name"] = name
            return AlertDestination.model_validate(data)
        return self._memory[name]

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
        """Remove a destination by name. Returns True if it existed."""
        if self._store is not None:
            yaml_path = Path(self._store._directory) / f"{name}.yaml"
            if yaml_path.exists():
                yaml_path.unlink()
                # Clear stale cache entry (_refresh_all only picks up new/changed files)
                with self._store._lock:
                    self._store._cache.pop(name, None)
                return True
            return False
        return self._memory.pop(name, None) is not None

    def list(self) -> list[AlertDestination]:
        """List all registered destinations."""
        if self._store is not None:
            results = []
            for table_name in self._store.list_tables():
                data = self._store.get(table_name)
                if data:
                    data["name"] = table_name
                    results.append(AlertDestination.model_validate(data))
            return results
        return list(self._memory.values())

    def resolve_many(self, names: list[str]) -> list[str]:
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

    def __len__(self) -> int:
        if self._store is not None:
            return len(list(self._store.list_tables()))
        return len(self._memory)

    def __contains__(self, name: str) -> bool:
        if self._store is not None:
            return self._store.get(name) is not None
        return name in self._memory


# ── Trigger Models ───────────────────────────────────────────────

_OPERATORS = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}


class AlertTrigger(BaseModel):
    """A condition that triggers an alert."""

    type: str = Field(..., description="Trigger type: any_match, result_count, field_value")
    operator: str = Field(default=">=", description="Comparison operator")
    value: Any = Field(default=None, description="Threshold or expected value")
    field: str | None = Field(default=None, description="Field name (for field_value triggers)")


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


# ── Dispatcher ────────────────────────────────────────────────────


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
        }

        # Enrich with group context if available
        if group_context:
            context["match_count"] = group_context.get("match_count", result_count)
            context["first_seen"] = group_context.get("first_seen", "")
            context["last_seen"] = group_context.get("last_seen", "")
            group_fields = group_context.get("group_fields", {})
            context["group_fields"] = ", ".join(
                f"{k}={v}" for k, v in group_fields.items()
            )

        title = self._config.title_template.format_map(_SafeFormatDict(context))
        body = self._config.body_template.format_map(_SafeFormatDict(context))

        return self._send(title, body)

    def _should_fire(
        self, result_count: int, results: list[dict[str, Any]] | None
    ) -> bool:
        """Check if any trigger condition is met."""
        for trigger in self._config.triggers:
            if trigger.type == "any_match":
                if result_count > 0:
                    return True

            elif trigger.type == "result_count":
                op_func = _OPERATORS.get(trigger.operator)
                if op_func and trigger.value is not None:
                    try:
                        if op_func(result_count, int(trigger.value)):
                            return True
                    except (ValueError, TypeError):
                        logger.warning(
                            f"Invalid result_count trigger value: {trigger.value}"
                        )

            elif trigger.type == "field_value":
                if trigger.field and results:
                    op_func = _OPERATORS.get(trigger.operator, operator.eq)
                    for row in results:
                        actual = row.get(trigger.field)
                        if actual is not None:
                            try:
                                if op_func(str(actual), str(trigger.value)):
                                    return True
                            except (ValueError, TypeError):
                                pass

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
            return result
        except Exception as e:
            logger.error(f"Alert dispatch error: {e}")
            return False


# ── Factory ───────────────────────────────────────────────────────


def build_alert_config(
    hunt_data: dict,
    global_channels: list[str] | None = None,
    destination_registry: AlertDestinationRegistry | None = None,
) -> AlertConfig | None:
    """Build AlertConfig from hunt YAML data, resolving named destinations.

    Hunt YAML format:
        alerts:
          destinations:          # named refs → resolved via registry
            - slack-dfe-alerts
            - pagerduty-oncall
          channels:              # raw Apprise URLs (backward compat)
            - mailto://...
          triggers:
            - type: any_match

    Resolution order:
    1. Resolve destination names via registry → Apprise URLs
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
        logger.warning(
            f"Hunt references destinations {destination_names} but no registry provided"
        )

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

    # Default to any_match if channels are configured but no triggers specified
    if not triggers:
        triggers = [AlertTrigger(type="any_match")]

    return AlertConfig(
        channels=channels,
        triggers=triggers,
        title_template=alerts_section.get("title_template", AlertConfig.model_fields["title_template"].default),
        body_template=alerts_section.get("body_template", AlertConfig.model_fields["body_template"].default),
        enabled=alerts_section.get("enabled", True),
    )
