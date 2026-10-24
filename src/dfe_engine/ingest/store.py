#  Project:      dfe-engine
#  File:         ingest/store.py
#  Purpose:      Keep compatibility profiles, their default bindings and the rule order in the deploy repo
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The deploy repo's half of the ingest rule set.

Profiles live in the ``compat_profiles`` class, one document per profile, and
the core ones are copied there from the pinned dfe-schemas wheel. The deployment's
default bindings and the rule order are two documents in ``governance/settings``.
A document that is absent reads as empty, so both always have an ETag.
"""

from typing import Any

from dfe_schemas.compat import load_compat_profile, profile_ids
from dfe_schemas.loader import SchemaError
from pydantic import ValidationError
from scalo.api import etag_of
from scalo.logger import logger

from dfe_engine.gitcrud import GitCrud, ResourceNotFoundError
from dfe_engine.gitops.repo import PublishResult
from dfe_engine.ingest.models import CompatProfile, CompatSetting, IngestRules, RuleOrder

PROFILES_CLASS = "compat_profiles"
SETTINGS_CLASS = "gov_settings"
COMPAT_SETTING = "compat"
ORDER_SETTING = "ingest-order"

ENGINE_ACTOR = "engine"


class CompatProfileNotFoundError(KeyError):
    """No compatibility profile has the id."""


def order_etag(order: RuleOrder) -> str:
    """The rule order's ETag, over both targets."""
    return etag_of(order.model_dump(mode="json", by_alias=True))


def compat_etag(setting: CompatSetting) -> str:
    """The deployment bindings' ETag."""
    return etag_of(setting.model_dump(mode="json"))


def profile_etag(profile: CompatProfile) -> str:
    """A profile document's ETag."""
    return etag_of(profile.model_dump(mode="json"))


class IngestStore:
    """Reads and writes the ingest rule set's documents through one deploy repo.

    Args:
        crud: The Governed Ops engine over the deploy repo.
    """

    def __init__(self, crud: GitCrud) -> None:
        self._crud = crud

    def read(self) -> IngestRules:
        """The order, profiles and default bindings as the deploy repo holds them now."""
        return IngestRules(
            order=self.order(),
            profiles=self.profiles(),
            defaults=tuple(self.compat().profiles),
        )

    def profiles(self) -> dict[str, CompatProfile]:
        """Every stored profile, by id. One that no longer parses is skipped loudly."""
        found: dict[str, CompatProfile] = {}
        for name in self._crud.list(PROFILES_CLASS):
            try:
                profile = CompatProfile.model_validate(self._crud.get(PROFILES_CLASS, name))
            except ValidationError as exc:
                logger.warning(
                    "compatibility profile skipped: it does not parse", profile=name, error=str(exc)
                )
                continue
            if profile.id != name:
                logger.warning(
                    "compatibility profile skipped: its id is not its name", profile=name
                )
                continue
            found[name] = profile
        return found

    def profile(self, profile_id: str) -> CompatProfile:
        """One stored profile.

        Raises:
            CompatProfileNotFoundError: No such profile is stored, or it does not parse.
        """
        held = self.profiles().get(profile_id)
        if held is None:
            raise CompatProfileNotFoundError(profile_id)
        return held

    def compat(self) -> CompatSetting:
        """The deployment's default bindings, empty when none are stored."""
        return CompatSetting.model_validate(self._setting(COMPAT_SETTING))

    def order(self) -> RuleOrder:
        """The rule order, empty when none is stored."""
        return RuleOrder.model_validate(self._setting(ORDER_SETTING))

    def order_at_head(self) -> tuple[RuleOrder, str | None]:
        """The rule order and the head it was read at, the base its conditional write takes."""
        doc, head = self._setting_at_head(ORDER_SETTING)
        return RuleOrder.model_validate(doc), head

    def compat_at_head(self) -> tuple[CompatSetting, str | None]:
        """The default bindings and the head they were read at."""
        doc, head = self._setting_at_head(COMPAT_SETTING)
        return CompatSetting.model_validate(doc), head

    def _setting(self, name: str) -> dict[str, Any]:
        try:
            return self._crud.get(SETTINGS_CLASS, name)
        except ResourceNotFoundError:
            return {}

    def _setting_at_head(self, name: str) -> tuple[dict[str, Any], str | None]:
        # One guard over both, so an absent document is absent at the head returned.
        with self._crud.reading():
            return self._setting(name), self._crud.head_revision()

    def put_order(
        self, order: RuleOrder, actor: str, *, base_revision: str | None
    ) -> PublishResult:
        """Store the rule order, conditional on the head it was read at.

        Raises:
            ConcurrencyConflictError: The head moved off *base_revision*.
        """
        return self._crud.put(
            SETTINGS_CLASS,
            ORDER_SETTING,
            order.model_dump(mode="json", by_alias=True),
            actor,
            message=f"ingest: reorder rules (by {actor})",
            base_revision=base_revision,
        )

    def put_compat(
        self, setting: CompatSetting, actor: str, *, base_revision: str | None
    ) -> PublishResult:
        """Store the deployment's default bindings, conditional on the head they were read at.

        Raises:
            ConcurrencyConflictError: The head moved off *base_revision*.
        """
        bindings = [b.model_dump(mode="json", exclude_defaults=True) for b in setting.profiles]
        return self._crud.put(
            SETTINGS_CLASS,
            COMPAT_SETTING,
            {"profiles": bindings},
            actor,
            message=f"ingest: bind compatibility profiles (by {actor})",
            base_revision=base_revision,
        )

    def seed_core_profiles(self) -> list[str]:
        """Copy every profile the pinned dfe-schemas ships into the deploy repo, in one commit.

        A stored copy already equal to the shipped one is left alone, and so is an
        operator's custom profile that holds a shipped id. A shipped profile that
        does not read, or sets a field the receiver does not, is skipped loudly.

        Returns:
            The ids written.
        """
        items: list[tuple[str, str, dict]] = []
        for profile_id in profile_ids():
            try:
                shipped = load_compat_profile(profile_id)
                profile = CompatProfile.model_validate(shipped)
            except (SchemaError, ValidationError) as exc:
                logger.warning(
                    "shipped compatibility profile not seeded", profile=profile_id, error=str(exc)
                )
                continue
            if profile.resource_type != "core":
                logger.warning(
                    "shipped compatibility profile not seeded: it is not core", profile=profile_id
                )
                continue
            try:
                stored = self._crud.get(PROFILES_CLASS, profile_id)
            except ResourceNotFoundError:
                stored = None
            if stored == shipped:
                continue
            if stored is not None and stored.get("resource_type") != "core":
                logger.warning(
                    "shipped compatibility profile not seeded: a custom profile holds its id",
                    profile=profile_id,
                )
                continue
            items.append((PROFILES_CLASS, profile_id, shipped))
        if items:
            self._crud.put_many(
                items, actor=ENGINE_ACTOR, message="ingest: seed compatibility profiles"
            )
        return [name for _, name, _ in items]
