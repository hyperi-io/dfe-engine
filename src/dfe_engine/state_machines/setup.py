#  Project:      dfe-engine
#  File:         state_machines/setup.py
#  Purpose:      First-run setup state machine driving the pre-login UI wizard
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""First-run setup state machine.

The control-plane UI polls this before login to decide whether to show the
first-run wizard, which step to land on, and what the deployment already has
configured.

Wizard order (declared in :data:`SETUP_STEPS`)::

    oidc_provider    optional   configure an external IdP
    organisations    required   create the first customer organisation
    first_user       required   create a real user (NOT the break-glass admin)
    admin_password   required   rotate the seeded break-glass admin password

A step applies when the thing it configures actually exists — not when a
settings toggle says so. ``app.py`` bootstraps the account store and seeds the
break-glass admin unconditionally, and ``POST /auth/login`` authenticates
against it with neither ``auth.enabled`` nor ``auth.local.enabled`` consulted.
So the seeded ``changeme`` credential is live even in a deployment that
believes auth is off, and the wizard has to say so.

``first_user`` may be satisfied by a local account or by an OIDC identity that
JIT-provisioned at first login — hence OIDC comes first, so an operator who
wants IdP-only users can configure it before creating anyone.

``admin_password`` is deliberately last: the break-glass credential is what
gets you through the earlier steps, so rotating it is the closing act.

The machine is pure. It reads a :class:`SetupContext` — never a Request — so
it can be evaluated in a unit test with hand-built stores.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from dfe_engine.auth.bootstrap import _DEFAULT_PASSWORD
from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.orgs.models import Org

if TYPE_CHECKING:
    from dfe_engine.auth.accounts import AccountStore
    from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
    from dfe_engine.orgs.registry import OrgRegistry

# The bootstrap-seeded break-glass admin (see auth/bootstrap.py::_seed_admin).
# It does not count as the "first user" — the whole point of that step is to
# get off the shared emergency credential and onto a real identity.
BREAK_GLASS_ACCOUNT = "admin"

# Stable step ids. These are an API contract: the UI keys its wizard screens
# off them, so treat a rename as a breaking change.
STEP_OIDC_PROVIDER = "oidc_provider"
STEP_ORGANISATIONS = "organisations"
STEP_FIRST_USER = "first_user"
STEP_ADMIN_PASSWORD = "admin_password"


# ── Context ──────────────────────────────────────────────────


@dataclass(frozen=True)
class SetupContext:
    """Everything the machine needs to evaluate a step, and nothing more.

    Deliberately no ``auth.enabled`` / ``auth.local.enabled``: neither toggle
    gates the login path, so neither one can tell you whether a setup step
    matters. The stores themselves can.
    """

    account_store: AccountStore | None = None
    org_registry: OrgRegistry | None = None
    oidc_registry: OIDCProviderRegistry | None = None

    @classmethod
    def from_app_state(cls, state: Any) -> SetupContext:
        """Build a context from bootstrapped FastAPI app state.

        Every store is optional: the app bootstraps them in stages and this
        endpoint is reachable while that is still in flight.

        Args:
            state: ``request.app.state`` (duck-typed to keep FastAPI out of here).

        Returns:
            A context describing the live deployment.
        """
        return cls(
            account_store=getattr(state, "account_store", None),
            org_registry=getattr(state, "org_registry", None),
            oidc_registry=getattr(state, "oidc_provider_registry", None),
        )


# ── Response models ──────────────────────────────────────────


class SetupStep(BaseModel):
    """One step of the first-run wizard."""

    id: str = Field(description="Stable step id (see STEP_* constants).")
    title: str = Field(description="Short label for the wizard screen.")
    description: str = Field(description="What the operator has to do in this step.")
    required: bool = Field(description="False for steps the operator may skip.")
    complete: bool = Field(description="True when this step is already satisfied.")


class InitialSetupState(BaseModel):
    """The machine's verdict on where first-run setup stands."""

    complete: bool = Field(
        description="True when every required step is satisfied.",
    )
    current_step: str | None = Field(
        default=None,
        description="First unsatisfied required step id — the screen to land on. "
        "None once setup is complete.",
    )
    steps: list[str] = Field(
        default_factory=list,
        description="All applicable step ids in wizard order (required and optional).",
    )
    pending_steps: list[str] = Field(
        default_factory=list,
        description="Required step ids still outstanding, in wizard order.",
    )
    completed_steps: list[str] = Field(
        default_factory=list,
        description="Satisfied step ids (required and optional), in wizard order.",
    )
    step_details: list[SetupStep] = Field(
        default_factory=list,
        description="Per-step title, description, required and complete flags.",
    )


class OIDCProviderName(BaseModel):
    """An OIDC provider by registry name alone.

    What a completed deployment serves: enough for the login screen to offer
    the provider, and nothing about how it is configured.
    """

    name: str = Field(description="Registry name (the provider YAML filename stem).")


class OIDCProviderSummary(OIDCProvider):
    """A registry OIDC provider, with its registry name folded in.

    ``OIDCProvider`` holds env var *names* rather than secret values, so the
    whole model is safe to return.
    """

    name: str = Field(description="Registry name (the provider YAML filename stem).")


class SetupStatus(BaseModel):
    """Pre-login setup snapshot: the wizard state plus what is configured.

    Accounts are deliberately absent. Whether a real user exists is reported
    through the ``first_user`` step; the account list itself is never served
    to an unauthenticated caller.
    """

    initial_setup: InitialSetupState = Field(
        description="Wizard state — completion, current step and per-step detail.",
    )
    oidc_providers: list[OIDCProviderSummary | OIDCProviderName] = Field(
        default_factory=list,
        description="The OIDC provider registry: full entries while setup is "
        "outstanding, then name-only entries for the enabled providers once it "
        "is complete, so the login screen can still offer them.",
    )
    organisations: list[Org] = Field(
        default_factory=list,
        description="The organisation registry. Empty once setup is complete.",
    )


# ── Step definitions ─────────────────────────────────────────


@dataclass(frozen=True)
class StepDefinition:
    """A step: when it applies, whether it is mandatory, and when it is done."""

    id: str
    title: str
    description: str
    applies: Callable[[SetupContext], bool]
    required: Callable[[SetupContext], bool]
    complete: Callable[[SetupContext], bool]


def _has_enabled_oidc_provider(ctx: SetupContext) -> bool:
    if ctx.oidc_registry is None:
        return False
    return any(provider.enabled for _, provider in ctx.oidc_registry.list())


def _has_organisation(ctx: SetupContext) -> bool:
    if ctx.org_registry is None:
        return False
    return len(ctx.org_registry.list()) > 0


def _has_real_user(ctx: SetupContext) -> bool:
    """True once an enabled account exists that is not the break-glass admin.

    Local or external (OIDC/JIT/SCIM-provisioned) both count — the step is
    about having a real identity, not about how it authenticates.
    """
    if ctx.account_store is None:
        return False
    return any(
        account.enabled and account.username != BREAK_GLASS_ACCOUNT
        for account in ctx.account_store.list()
    )


def _has_break_glass_account(ctx: SetupContext) -> bool:
    """True when the bootstrap-seeded admin account is on disk.

    ``bootstrap_auth`` seeds it on every startup, so this is the honest test
    of whether there is a shared emergency credential to rotate.
    """
    if ctx.account_store is None:
        return False
    return ctx.account_store.get(BREAK_GLASS_ACCOUNT) is not None


def _break_glass_password_rotated(ctx: SetupContext) -> bool:
    """True once the seeded admin no longer answers to the default password.

    Tests the *default* password specifically: an operator who set
    DFE_AUTH_LOCAL_ADMIN_PASSWORD at bootstrap never had a shared secret to
    rotate, so the step is already satisfied.

    Costs one bcrypt verify per call on an unauthenticated endpoint. There is
    no cheaper honest test — a changed ``updated_at`` also fires for an
    enabled/groups edit, which would report the rotation as done while the
    default password still worked.
    """
    if ctx.account_store is None:
        return False
    return not ctx.account_store.verify_password(BREAK_GLASS_ACCOUNT, _DEFAULT_PASSWORD)


SETUP_STEPS: tuple[StepDefinition, ...] = (
    StepDefinition(
        id=STEP_OIDC_PROVIDER,
        title="Connect an identity provider",
        description=(
            "Register an OIDC provider so users sign in with your IdP. "
            "Optional — local accounts work without it."
        ),
        applies=lambda ctx: ctx.oidc_registry is not None,
        required=lambda _ctx: False,
        complete=_has_enabled_oidc_provider,
    ),
    StepDefinition(
        id=STEP_ORGANISATIONS,
        title="Create your first organisation",
        description=(
            "Organisations scope tenant data and ClickHouse row-level security. "
            "At least one is needed before data can be onboarded."
        ),
        applies=lambda ctx: ctx.org_registry is not None,
        required=lambda _ctx: True,
        complete=_has_organisation,
    ),
    StepDefinition(
        id=STEP_FIRST_USER,
        title="Create your first user",
        description=(
            "Add a real user — local or from your IdP — separate from the "
            "break-glass admin account, so day-to-day work is attributable."
        ),
        applies=lambda ctx: ctx.account_store is not None,
        required=lambda _ctx: True,
        complete=_has_real_user,
    ),
    StepDefinition(
        id=STEP_ADMIN_PASSWORD,
        title="Rotate the break-glass admin password",
        description=(
            "The bootstrapped admin account still uses its default password. "
            "Change it — it is the emergency credential for this deployment."
        ),
        applies=_has_break_glass_account,
        required=lambda _ctx: True,
        complete=_break_glass_password_rotated,
    ),
)


# ── The machine ──────────────────────────────────────────────


class SetupStateMachine:
    """Evaluates :data:`SETUP_STEPS` against a live deployment."""

    def __init__(self, steps: tuple[StepDefinition, ...] = SETUP_STEPS) -> None:
        self._steps = steps

    def evaluate(self, ctx: SetupContext) -> InitialSetupState:
        """Resolve every applicable step and report where setup stands.

        Args:
            ctx: Live deployment state.

        Returns:
            Completion, the step to land on, and per-step detail in wizard order.
        """
        details = [
            SetupStep(
                id=step.id,
                title=step.title,
                description=step.description,
                required=step.required(ctx),
                complete=step.complete(ctx),
            )
            for step in self._steps
            if step.applies(ctx)
        ]

        pending = [s.id for s in details if s.required and not s.complete]
        return InitialSetupState(
            complete=not pending,
            current_step=pending[0] if pending else None,
            steps=[s.id for s in details],
            pending_steps=pending,
            completed_steps=[s.id for s in details if s.complete],
            step_details=details,
        )

    def status(self, ctx: SetupContext, *, redact_when_complete: bool = True) -> SetupStatus:
        """Build the full pre-login snapshot: wizard state plus the registries.

        The org and OIDC registries are what the wizard renders, and this
        endpoint is unauthenticated. With ``redact_when_complete`` they are
        returned in full only while setup is still outstanding (a fresh
        deployment, where there is nothing yet to disclose) and cut back once
        it is done, so a configured deployment does not serve its org and IdP
        inventory to anonymous callers. Accounts are never included at all —
        the ``first_user`` step reports whether one exists.

        The one thing that survives completion is the *name* of each enabled
        OIDC provider: the login screen has to know which IdPs to offer, and a
        name alone discloses no configuration. Disabled providers drop out —
        they cannot be logged in with, so listing them would be inventory
        disclosure with nothing to render.

        Args:
            ctx: Live deployment state.
            redact_when_complete: Reduce the registries once setup is complete.
                Set False to always include them in full.

        Returns:
            The snapshot the ``/auth/setup-status`` endpoint returns.
        """
        state = self.evaluate(ctx)
        if redact_when_complete and state.complete:
            return SetupStatus(
                initial_setup=state,
                oidc_providers=self._enabled_oidc_provider_names(ctx),
            )

        return SetupStatus(
            initial_setup=state,
            oidc_providers=self._oidc_providers(ctx),
            organisations=self._organisations(ctx),
        )

    # ------------------------------------------------------------------
    # Registry projections
    # ------------------------------------------------------------------

    @staticmethod
    def _oidc_providers(ctx: SetupContext) -> list[OIDCProviderSummary]:
        if ctx.oidc_registry is None:
            return []
        return [
            OIDCProviderSummary(name=name, **provider.model_dump())
            for name, provider in ctx.oidc_registry.list()
        ]

    @staticmethod
    def _enabled_oidc_provider_names(ctx: SetupContext) -> list[OIDCProviderName]:
        if ctx.oidc_registry is None:
            return []
        return [
            OIDCProviderName(name=name)
            for name, provider in ctx.oidc_registry.list()
            if provider.enabled
        ]

    @staticmethod
    def _organisations(ctx: SetupContext) -> list[Org]:
        if ctx.org_registry is None:
            return []
        return list(ctx.org_registry.list())


SETUP_MACHINE = SetupStateMachine()
"""Shared instance — the machine is stateless, so one is enough."""
