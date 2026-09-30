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
    first_user       required   create a real user (NOT a seeded credential)

A step applies when the thing it configures actually exists -- not when a
settings toggle says so. ``app.py`` bootstraps the account store and seeds the
local admin unconditionally, and ``POST /auth/login`` authenticates against it
with neither ``auth.enabled`` nor ``auth.local.enabled`` consulted. So the
seeded credential is live even in a deployment that believes auth is off, and
the wizard has to say so.

``first_user`` may be satisfied by a local account or by an OIDC identity that
JIT-provisioned at first login -- hence OIDC comes first, so an operator who
wants IdP-only users can configure it before creating anyone.

The admin password is not a wizard step: every deployment mints one at deploy
time and the engine refuses to start on the shipped default outside a dev
posture. ``default_credentials`` reports the dev deployment that is still on it,
for the UI to banner.

The status also carries ``deploy_kind`` and ``credential_fetch_command``, which
the pre-login page shows so an operator can read the password their deployment
minted rather than guessing at one.

``admin_retired`` and ``retire_admin_available`` close that loop: once the
deployment has an admin of its own, the operator retires the bootstrap admin
(:mod:`dfe_engine.auth.admin_retirement`) and deletes the minted password from
the Secret or ``.env``.

The machine is pure. It reads a :class:`SetupContext` -- never a Request -- so
it can be evaluated in a unit test with hand-built stores.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

from pydantic import BaseModel, Field

from dfe_engine.auth import admin_retirement
from dfe_engine.auth.account_durability import AccountGitState
from dfe_engine.auth.bootstrap import (
    admin_account_name,
    admin_account_password,
    admin_on_default_password,
)
from dfe_engine.auth.breakglass import USERNAME as BREAKGLASS_USERNAME
from dfe_engine.auth.deployment_hints import (
    credential_fetch_command,
    detect_deploy_kind,
)
from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.gitcrud import retention
from dfe_engine.orgs.models import Org

if TYPE_CHECKING:
    from dfe_engine.auth.accounts import AccountStore
    from dfe_engine.auth.groups import GroupStore
    from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
    from dfe_engine.orgs.registry import OrgRegistry

# The bootstrap-seeded LOCAL ADMIN (see auth/bootstrap.py::_seed_admin) does not
# count as the "first user" -- that step is about getting off the shared admin
# credential and onto a real identity.

# Stable step ids. These are an API contract: the UI keys its wizard screens
# off them, so treat a rename as a breaking change.
STEP_OIDC_PROVIDER = "oidc_provider"
STEP_ORGANISATIONS = "organisations"
STEP_FIRST_USER = "first_user"


# -- Context --------------------------------------------------


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
    group_store: GroupStore | None = None
    org_registry: OrgRegistry | None = None
    oidc_registry: OIDCProviderRegistry | None = None
    break_glass_git: AccountGitState | None = None
    # The configured local admin; its password is what ``default_credentials`` grades.
    bootstrap_admin_name: str = field(default_factory=admin_account_name)
    bootstrap_admin_password: str = field(default_factory=admin_account_password)
    # Where the operator reads the minted password, for the pre-login page.
    deploy_kind: str = ""
    credential_fetch_command: str = ""
    # Effective retention a source gets when it sets none (override else env); 0 = none.
    default_ttl_days: int = 90
    # MergeTree-family variant a source's table gets when it names no engine.
    default_engine: str = "MergeTree"
    # The deploy repo says the bootstrap admin is retired: not seeded, stays disabled.
    admin_retired: bool = False
    # A deploy repo is configured, so a retirement has somewhere durable to be recorded.
    gitops_enabled: bool = False
    # auth.source_provider_bindings, which decide the groups an IdP-owned account holds.
    provider_bindings: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_app_state(cls, state: Any) -> Self:
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
        default_ttl_days = 90
        clickhouse = _attr_path(state, "settings", "clickhouse")
        # The schema builder treats a blank default as MergeTree, so report the same.
        default_engine = getattr(clickhouse, "default_engine", "") or "MergeTree"
        if clickhouse is not None:
            default_ttl_days = retention.resolve_state(
                getattr(state, "gitcrud", None), state.settings
            ).effective
        return cls(
            account_store=account_store,
            group_store=getattr(state, "group_store", None),
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
            default_engine=default_engine,
            default_ttl_days=default_ttl_days,
            admin_retired=admin_retirement.is_retired(getattr(state, "gitcrud", None)),
            gitops_enabled=getattr(state, "gitcrud", None) is not None,
            provider_bindings=dict(
                _attr_path(state, "settings", "auth", "source_provider_bindings") or {}
            ),
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


# -- Response models ------------------------------------------


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
        description="First unsatisfied required step id -- the screen to land on. "
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
        "provider does not set one -- fall back to ``name``.",
    )


class OIDCProviderSummary(OIDCProvider):
    """A registry OIDC provider, with its registry name folded in.

    ``OIDCProvider`` holds secret-store paths and env var *names* rather than
    secret values, and the client id is public by design (the browser carries it
    to the IdP authorize endpoint), so the whole model is safe to return.
    """

    name: str = Field(description="Registry name (the provider YAML filename stem).")


class SetupStatus(BaseModel):
    """Pre-login setup snapshot: the wizard state plus what is configured.

    Accounts are deliberately absent. Whether a real user exists is reported
    through the ``first_user`` step; the account list itself is never served
    to an unauthenticated caller.
    """

    initial_setup: InitialSetupState = Field(
        description="Wizard state -- completion, current step and per-step detail.",
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
        "enabled/auto_merge/committed/merged. pending is always null here: "
        "the review PR URL or CLI merge command comes only in the "
        "reset-password response. committed without merged means a change "
        "is still waiting on that merge.",
    )
    default_credentials: bool = Field(
        default=False,
        description="True while the local admin still logs in on the shipped default "
        "password, read at this request, so the admin's own change clears it at once. "
        "Only reachable in a dev posture -- the engine refuses to start on it "
        "otherwise -- so the UI banners and forces a change.",
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
    default_engine: str = Field(
        default="MergeTree",
        description="MergeTree-family engine variant a source's table gets when its "
        "schema names none (DFE_CLICKHOUSE_DEFAULT_ENGINE). The topology prefix "
        "(Replicated, Shared) is resolved against the server at DDL time, not here.",
    )
    admin_username: str = Field(
        default="",
        description="The bootstrap admin's account name, which the deployment may "
        "rename. The console names it in the retire prompt and marks it retired in "
        "the account list, so it cannot guess at 'admin'.",
    )
    admin_retired: bool = Field(
        default=False,
        description="True when the bootstrap admin has been retired: the deploy repo "
        "carries the fact, the account is disabled and never reseeded, and the minted "
        "password may be deleted from the Secret or .env.",
    )
    retire_admin_available: bool = Field(
        default=False,
        description="True when retiring the bootstrap admin would be accepted now: "
        "setup is complete, an enabled admin-role account other than the seeded pair "
        "exists, and the admin is not retired yet. The wizard enables its button on it.",
    )


# -- Step definitions -----------------------------------------


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
    """True once an enabled account exists that is neither seeded credential.

    Local or external (OIDC/JIT/SCIM-provisioned) both count -- the step is
    about having a real identity, not about how it authenticates.

    Neither the local admin nor ``breakglass`` clears it. Both are seeded by
    ``bootstrap_auth`` rather than created by an operator, and break-glass is a
    shared recovery credential, so a deployment holding only those two has
    nobody to attribute day-to-day work to.
    """
    if ctx.account_store is None:
        return False
    seeded = {ctx.bootstrap_admin_name, BREAKGLASS_USERNAME}
    return any(
        account.enabled and account.username not in seeded for account in ctx.account_store.list()
    )


def default_credentials(ctx: SetupContext) -> bool:
    """True while the local admin still logs in on the shipped admin password.

    The same verdict the login response carries (:func:`admin_on_default_password`),
    so the admin's own change clears both at once.
    """
    return admin_on_default_password(
        ctx.account_store, ctx.bootstrap_admin_name, ctx.bootstrap_admin_password
    )


SETUP_STEPS: tuple[StepDefinition, ...] = (
    StepDefinition(
        id=STEP_OIDC_PROVIDER,
        title="Connect an identity provider",
        description=(
            "Register an OIDC provider so users sign in with your IdP. "
            "Optional -- local accounts work without it."
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
            "Add a real user -- local or from your IdP -- separate from the "
            "shared admin account, so day-to-day work is attributable."
        ),
        applies=lambda ctx: ctx.account_store is not None,
        required=lambda _ctx: True,
        complete=_has_real_user,
    ),
)


# -- The machine ----------------------------------------------


def retire_admin_available(ctx: SetupContext, setup_complete: bool) -> bool:
    """Whether retiring the bootstrap admin would be accepted right now.

    ONE predicate for the wizard's hint and the endpoint's refusal, so the UI
    cannot offer a button the API answers with a 409. Retirement needs somewhere
    durable to record the fact, so a deployment without a deploy repo never
    qualifies.
    """
    return (
        setup_complete
        and ctx.gitops_enabled
        and not ctx.admin_retired
        and admin_retirement.another_admin_exists(
            ctx.account_store,
            ctx.group_store,
            ctx.bootstrap_admin_name,
            bindings=ctx.provider_bindings,
        )
    )


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
        inventory to anonymous callers. Accounts are never included at all --
        the ``first_user`` step reports whether one exists.

        What survives completion is the name and display name of each enabled
        OIDC provider: the login screen has to know which IdPs to offer and
        what to call them, and neither discloses any configuration. Disabled
        providers drop out -- they cannot be logged in with, so listing them
        would be inventory disclosure with nothing to render.

        ``deploy_kind`` and ``credential_fetch_command`` survive too (#301). The
        login page shows the fetch command to an operator who has lost the admin
        password, which is exactly the case that arises long after setup is
        complete. It names where the password is kept, never the password --
        running it needs cluster or host credentials of its own.

        Args:
            ctx: Live deployment state.
            redact_when_complete: Reduce the registries once setup is complete.
                Set False to always include them in full.

        Returns:
            The snapshot the ``/auth/setup-status`` endpoint returns.
        """
        state = self.evaluate(ctx)
        status = SetupStatus(
            initial_setup=state,
            oidc_providers=self._oidc_providers(ctx),
            organisations=self._organisations(ctx),
            break_glass=ctx.break_glass_git,
            default_credentials=default_credentials(ctx),
            deploy_kind=ctx.deploy_kind,
            credential_fetch_command=ctx.credential_fetch_command,
            default_engine=ctx.default_engine,
            default_ttl_days=ctx.default_ttl_days,
            admin_username=ctx.bootstrap_admin_name,
            admin_retired=ctx.admin_retired,
            retire_admin_available=retire_admin_available(ctx, state.complete),
        )
        if not (redact_when_complete and state.complete):
            return status
        # One construction, then the registries are cut back: a second full one
        # drifts, and a field added to only one of them is a silent contract gap.
        return status.model_copy(
            update={
                "oidc_providers": self._enabled_oidc_login_options(ctx),
                "organisations": [],
            }
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
"""Shared instance -- the machine is stateless, so one is enough."""
