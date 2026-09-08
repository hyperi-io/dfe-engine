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
    first_user       required   create a real user (NOT the local admin)
    admin_password   required   move off the shipped default admin password

A step applies when the thing it configures actually exists — not when a
settings toggle says so. ``app.py`` bootstraps the account store and seeds the
local admin unconditionally, and ``POST /auth/login`` authenticates against it
with neither ``auth.enabled`` nor ``auth.local.enabled`` consulted. So the
seeded credential is live even in a deployment that believes auth is off, and
the wizard has to say so.

``first_user`` may be satisfied by a local account or by an OIDC identity that
JIT-provisioned at first login — hence OIDC comes first, so an operator who
wants IdP-only users can configure it before creating anyone.

``admin_password`` is deliberately last: the admin credential is what gets you
through the earlier steps. It only ever appears outstanding in a dev posture —
the engine refuses to start on the default password anywhere else — so the
step exists to chase a tyre-kicker deployment off ``changeme``.

The status also carries ``deploy_kind`` and ``credential_fetch_command``, which
the pre-login page shows so an operator can read the password their deployment
minted rather than guessing at one.

The machine is pure. It reads a :class:`SetupContext` — never a Request — so
it can be evaluated in a unit test with hand-built stores.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from dfe_engine.auth.account_durability import AccountGitState
from dfe_engine.auth.bootstrap import (
    admin_account_name,
    admin_account_password,
    default_credentials_in_use,
)
from dfe_engine.auth.deployment_hints import (
    credential_fetch_command,
    detect_deploy_kind,
)
from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.orgs.models import Org

if TYPE_CHECKING:
    from dfe_engine.auth.accounts import AccountStore
    from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
    from dfe_engine.orgs.registry import OrgRegistry

# The bootstrap-seeded LOCAL ADMIN (see auth/bootstrap.py::_seed_admin), as
# configured in settings.auth.local, so rotation is measured against what was seeded.
# It does not count as the "first user" — that step is about getting off the shared
# admin credential and onto a real identity.

# Stable step ids. These are an API contract: the UI keys its wizard screens
# off them, so treat a rename as a breaking change.
STEP_OIDC_PROVIDER = "oidc_provider"
STEP_ORGANISATIONS = "organisations"
STEP_FIRST_USER = "first_user"
STEP_ADMIN_PASSWORD = "admin_password"


# ── Context ──────────────────────────────────────────────────


def _attr_path(root: Any, *names: str) -> Any:
    """Walk an attribute chain, stopping at the first name that is absent or None."""
    value = root
    for name in names:
        value = getattr(value, name, None)
        if value is None:
            return None
    return value


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
    break_glass_git: AccountGitState | None = None
    # The configured local admin; the default password is what the wizard chases off.
    bootstrap_admin_name: str = field(default_factory=admin_account_name)
    bootstrap_admin_password: str = field(default_factory=admin_account_password)
    # Where the operator reads the minted password, for the pre-login page.
    deploy_kind: str = ""
    credential_fetch_command: str = ""
    # Retention a source gets when it sets none; 0 = none.
    default_ttl_days: int = 90

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
        account_store = getattr(state, "account_store", None)
        local = _attr_path(state, "settings", "auth", "local")
        admin_name = admin_account_name(getattr(local, "admin_name", "") or "")
        deployment = _attr_path(state, "settings", "deployment")
        kind = detect_deploy_kind(getattr(deployment, "target", "") or "")
        clickhouse = _attr_path(state, "settings", "clickhouse")
        return cls(
            account_store=account_store,
            org_registry=getattr(state, "org_registry", None),
            oidc_registry=getattr(state, "oidc_provider_registry", None),
            break_glass_git=_break_glass_git_state(state, account_store, admin_name),
            bootstrap_admin_name=admin_name,
            bootstrap_admin_password=admin_account_password(
                getattr(local, "admin_password", "") or ""
            ),
            deploy_kind=kind,
            credential_fetch_command=credential_fetch_command(
                kind,
                namespace=getattr(deployment, "namespace", "") or "",
                secret_name=getattr(local, "admin_secret_name", "") or "",
                secret_key=getattr(local, "admin_secret_key", "") or "",
            ),
            default_ttl_days=int(getattr(clickhouse, "default_ttl_days", 90) or 0),
        )


def _break_glass_git_state(
    state: Any, account_store: AccountStore | None, admin_name: str
) -> AccountGitState | None:
    """Durability of the break-glass admin password in the deploy repo, or None.

    None while auth or settings are still bootstrapping; otherwise the steady-state
    git status for the seeded admin so the wizard can show "persisted" vs a pending
    review PR (including the merge command / PR URL). Pre-login safe: no secrets.
    """
    if account_store is None:
        return None
    settings = getattr(state, "settings", None)
    if settings is None:
        return None
    from dfe_engine.auth.account_durability import steady_state

    return steady_state(
        getattr(state, "gitcrud", None),
        account_store,
        admin_name,
        environment=settings.env,
        mode=settings.gitops.mode,
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


class OIDCProviderLoginOption(BaseModel):
    """An OIDC provider reduced to what a login button needs.

    What a completed deployment serves: enough for the login screen to offer
    the provider, and nothing about how it is configured.
    """

    name: str = Field(description="Registry name (the provider YAML filename stem).")
    display_name: str = Field(
        default="",
        description="Human-readable label for the login button. Empty when the "
        "provider does not set one — fall back to ``name``.",
    )


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
    oidc_providers: list[OIDCProviderSummary | OIDCProviderLoginOption] = Field(
        default_factory=list,
        description="The OIDC provider registry: full entries while setup is "
        "outstanding, then name and display name only for the enabled providers "
        "once it is complete, so the login screen can still offer them.",
    )
    organisations: list[Org] = Field(
        default_factory=list,
        description="The organisation registry. Empty once setup is complete.",
    )
    # The JSON name stays break_glass: dfe-ui reads it. It reports the local admin.
    break_glass: AccountGitState | None = Field(
        default=None,
        description="Durability of the LOCAL ADMIN account in the deploy repo "
        "(the field name predates the separate breakglass recovery account): "
        "enabled/auto_merge/committed/merged, plus pending.pr_url/command/"
        "branch when a review PR or CLI merge is still outstanding.",
    )
    default_credentials: bool = Field(
        default=False,
        description="True when the deployment is running on the shipped default "
        "admin password. Only reachable in a dev posture -- the engine refuses to "
        "start on it otherwise -- so the UI banners and forces a change.",
    )
    deploy_kind: str = Field(
        default="",
        description="docker | kubernetes | local. The deployment vehicle, injected "
        "by the deployer where it says so and detected from the runtime otherwise.",
    )
    credential_fetch_command: str = Field(
        default="",
        description="One-line command that prints this deployment's minted admin "
        "password, for the login page to show. Carried after setup completes too "
        "-- an operator who has lost the password needs it most then.",
    )
    default_ttl_days: int = Field(
        description="Retention in days a source gets when it sets none; 0 = none.",
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
    """True once an enabled account exists that is not the local admin.

    Local or external (OIDC/JIT/SCIM-provisioned) both count — the step is
    about having a real identity, not about how it authenticates.
    """
    if ctx.account_store is None:
        return False
    return any(
        account.enabled and account.username != ctx.bootstrap_admin_name
        for account in ctx.account_store.list()
    )


def _has_local_admin_account(ctx: SetupContext) -> bool:
    """True when the bootstrap-seeded local admin account is on disk.

    Not the ``breakglass`` recovery account, which is a separate identity with
    its own hash in the deploy repo. ``bootstrap_auth`` seeds this one on every
    startup, so it is the honest test of whether there is an admin password to
    move off the default.
    """
    if ctx.account_store is None:
        return False
    return ctx.account_store.get(ctx.bootstrap_admin_name) is not None


def default_credentials(ctx: SetupContext) -> bool:
    """True when the deployment is still running on the shipped admin password.

    Read from the configured password, not from a bcrypt verify: the admin is
    reconciled from that config on every boot, so config is what the deployment
    is actually running on. A runtime change that config does not carry is
    reasserted at the next start.
    """
    return default_credentials_in_use(ctx.bootstrap_admin_password)


def _admin_password_step_complete(ctx: SetupContext) -> bool:
    """The deployment minted its own admin password rather than shipping on the default."""
    return not default_credentials(ctx)


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
            "shared admin account, so day-to-day work is attributable."
        ),
        applies=lambda ctx: ctx.account_store is not None,
        required=lambda _ctx: True,
        complete=_has_real_user,
    ),
    StepDefinition(
        id=STEP_ADMIN_PASSWORD,
        title="Move off the default admin password",
        description=(
            "The admin account is still on the shipped default password. Mint a "
            "password in the deployment's secret store and inject it as "
            "DFE_AUTH_LOCAL_ADMIN_PASSWORD — the engine reasserts that value on "
            "every boot, so a password set anywhere else is reverted."
        ),
        applies=_has_local_admin_account,
        required=lambda _ctx: True,
        complete=_admin_password_step_complete,
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

        What survives completion is the name and display name of each enabled
        OIDC provider: the login screen has to know which IdPs to offer and
        what to call them, and neither discloses any configuration. Disabled
        providers drop out — they cannot be logged in with, so listing them
        would be inventory disclosure with nothing to render.

        ``deploy_kind`` and ``credential_fetch_command`` survive too (#301). The
        login page shows the fetch command to an operator who has lost the admin
        password, which is exactly the case that arises long after setup is
        complete. It names where the password is kept, never the password —
        running it needs cluster or host credentials of its own.

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
                oidc_providers=self._enabled_oidc_login_options(ctx),
                break_glass=ctx.break_glass_git,
                default_credentials=default_credentials(ctx),
                deploy_kind=ctx.deploy_kind,
                credential_fetch_command=ctx.credential_fetch_command,
                default_ttl_days=ctx.default_ttl_days,
            )

        return SetupStatus(
            initial_setup=state,
            oidc_providers=self._oidc_providers(ctx),
            organisations=self._organisations(ctx),
            break_glass=ctx.break_glass_git,
            default_credentials=default_credentials(ctx),
            deploy_kind=ctx.deploy_kind,
            credential_fetch_command=ctx.credential_fetch_command,
            default_ttl_days=ctx.default_ttl_days,
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
    def _enabled_oidc_login_options(ctx: SetupContext) -> list[OIDCProviderLoginOption]:
        if ctx.oidc_registry is None:
            return []
        return [
            OIDCProviderLoginOption(name=name, display_name=provider.display_name)
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
