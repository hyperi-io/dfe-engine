#  Project:      dfe-engine
#  File:         orgs/tenancy.py
#  Purpose:      The tenancy facts the engine compiles into every data-plane app
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""What the data-plane apps are told about tenancy.

A record's tenant is its ``_org_id``. A record that names none takes your org's first
tenant id, the organisation running the deployment, so a hunt for your org reads it.
With no org named as yours it takes ``unassigned``, which no org may hold. Every app
carries ``default_org_id`` and the engine compiles the same value into each of them.
dfe-receiver also carries ``ingest_bindings``, the orgs each ingest credential's
records may land in, and dfe-fetcher the one tenant the source it polls belongs to.
All of it is compiled from a :class:`Tenancy` read once per compile, so the apps
never disagree about which org a tenant id is.
"""

from dataclasses import dataclass
from typing import Any

from scalo.logger import logger

from dfe_engine.orgs.ingest_credentials import IngestCredential
from dfe_engine.orgs.models import UNASSIGNED_ORG_ID, Org
from dfe_engine.orgs.registry import OrgRegistry
from dfe_engine.orgs.tenant_scope import org_tenant_ids


class UnknownOrgError(KeyError):
    """A compile names an org the registry does not hold."""


@dataclass(frozen=True, slots=True)
class Tenancy:
    """The orgs, ingest credentials and your org that one compile reads.

    Attributes:
        orgs: Every registered org.
        credentials: Every ingest credential.
        home: Your org's name, None while none is named.
    """

    orgs: tuple[Org, ...] = ()
    credentials: tuple[IngestCredential, ...] = ()
    home: str | None = None

    @classmethod
    def read(cls, registry: OrgRegistry) -> Tenancy:
        """The registry's orgs, ingest credentials and your org, as they are now.

        An unreadable your-org doc reads as none named, so the compile still runs and
        a record naming no org lands under ``unassigned`` until the doc is replaced.
        """
        try:
            home = registry.home_name()
        except ValueError as exc:
            logger.warning(
                "your organisation cannot be read, records naming no org land unassigned",
                error=str(exc),
            )
            home = None
        return cls(tuple(registry.list()), tuple(registry.ingest_credentials.list()), home)

    def default_tenant_id(self) -> str:
        """The ``_org_id`` a record naming no org takes: your org's first tenant id.

        ``unassigned`` while no org is named as yours, or the one named is not a
        registered, enabled org, the only orgs a record may land in.
        """
        for registered in self.orgs:
            if registered.name == self.home and registered.enabled:
                return org_tenant_ids(registered)[0]
        return UNASSIGNED_ORG_ID

    def org(self, name: str) -> Org:
        """The registered org named *name*.

        Raises:
            UnknownOrgError: No registered org is named *name*.
        """
        for registered in self.orgs:
            if registered.name == name:
                return registered
        raise UnknownOrgError(name)

    def tenant_ids(self, org: str) -> list[str]:
        """The ``_org_id`` values the org named *org* owns, in declared order.

        Raises:
            UnknownOrgError: No registered org is named *org*.
        """
        return org_tenant_ids(self.org(org))

    def tenant_id(self, org: str | None) -> str:
        """The one ``_org_id`` a record bound to *org* is stamped with: its first tenant id.

        Args:
            org: An org name, or None for a record bound to no org (:meth:`default_tenant_id`).

        Raises:
            UnknownOrgError: No registered org is named *org*.
        """
        if org is None:
            return self.default_tenant_id()
        return self.tenant_ids(org)[0]


def default_tenant_id(tenancy: Tenancy | None) -> str:
    """The ``_org_id`` a record naming no org takes, ``unassigned`` for a caller holding no tenancy."""
    return tenancy.default_tenant_id() if tenancy is not None else UNASSIGNED_ORG_ID


def _binding(tenancy: Tenancy, credential: IngestCredential) -> dict[str, Any]:
    """One credential's ``ingest_bindings`` entry, as the tenant ids of its enabled orgs.

    A disabled default gives way to the owning org, which the caller has checked is
    enabled, so the default is always one of ``orgs``.

    Raises:
        UnknownOrgError: The credential binds an org the registry does not hold.
    """
    orgs: list[str] = []
    for name in credential.orgs:
        if tenancy.org(name).enabled:
            orgs.extend(tenancy.tenant_ids(name))
    default = credential.default_org
    if not tenancy.org(default).enabled:
        default = credential.org
    return {"orgs": list(dict.fromkeys(orgs)), "default_org": tenancy.tenant_id(default)}


def compile_ingest_bindings(tenancy: Tenancy) -> dict[str, Any]:
    """dfe-receiver's ``ingest_bindings`` block: each credential's enabled orgs as tenant ids.

    A credential binding an org the registry no longer holds is left out, so its
    records take ``default_org_id`` alone (:meth:`Tenancy.default_tenant_id`), never the
    org that is gone. So is a credential whose owning org is disabled, whose token no listener accepts. A
    disabled org is left out of every other binding, so no record lands in it.

    Args:
        tenancy: The orgs and credentials to compile.

    Returns:
        ``{"credentials": {<id>: {"orgs": [...], "default_org": ...}}}``, ids sorted.
    """
    credentials: dict[str, Any] = {}
    for credential in sorted(tenancy.credentials, key=lambda c: c.name):
        try:
            if not tenancy.org(credential.org).enabled:
                continue
            credentials[credential.name] = _binding(tenancy, credential)
        except UnknownOrgError as exc:
            logger.warning(
                "ingest credential left unbound: it names an org that is not registered",
                ingest_id=credential.name,
                org=str(exc),
            )
    return {"credentials": credentials}
