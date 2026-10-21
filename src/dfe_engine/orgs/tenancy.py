#  Project:      dfe-engine
#  File:         orgs/tenancy.py
#  Purpose:      The tenancy facts the engine compiles into every data-plane app
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""What the data-plane apps are told about tenancy.

A record's tenant is its ``_org_id``. A record that names none lands in the
default org, so every app carries ``default_org_id`` and the engine compiles the
same value into each of them. dfe-receiver also carries ``ingest_bindings``, the
orgs each ingest credential's records may land in, and dfe-fetcher the one tenant
the source it polls belongs to. All of it is compiled from a :class:`Tenancy`
read once per compile, so the apps never disagree about which org a tenant id is.
"""

from dataclasses import dataclass
from typing import Any

from scalo.logger import logger

from dfe_engine.orgs.ingest_credentials import IngestCredential
from dfe_engine.orgs.models import DEFAULT_ORG_ID, Org
from dfe_engine.orgs.registry import OrgRegistry
from dfe_engine.orgs.tenant_scope import org_tenant_ids


class UnknownOrgError(KeyError):
    """A compile names an org the registry does not hold."""


@dataclass(frozen=True, slots=True)
class Tenancy:
    """The orgs and ingest credentials one compile reads.

    Attributes:
        orgs: Every registered org.
        credentials: Every ingest credential.
    """

    orgs: tuple[Org, ...] = ()
    credentials: tuple[IngestCredential, ...] = ()

    @classmethod
    def read(cls, registry: OrgRegistry) -> Tenancy:
        """The registry's orgs and ingest credentials, as they are now."""
        return cls(tuple(registry.list()), tuple(registry.ingest_credentials.list()))

    def tenant_ids(self, org: str) -> list[str]:
        """The ``_org_id`` values the org named *org* owns, in declared order.

        Raises:
            UnknownOrgError: No registered org is named *org*.
        """
        for registered in self.orgs:
            if registered.name == org:
                return org_tenant_ids(registered)
        raise UnknownOrgError(org)

    def tenant_id(self, org: str | None) -> str:
        """The one ``_org_id`` a record bound to *org* is stamped with: its first tenant id.

        Args:
            org: An org name, or None for the default org.

        Raises:
            UnknownOrgError: No registered org is named *org*.
        """
        if org is None:
            return DEFAULT_ORG_ID
        return self.tenant_ids(org)[0]


def _binding(tenancy: Tenancy, credential: IngestCredential) -> dict[str, Any]:
    """One credential's ``ingest_bindings`` entry, as tenant ids.

    Raises:
        UnknownOrgError: The credential binds an org the registry does not hold.
    """
    orgs: list[str] = []
    for org in credential.orgs:
        orgs.extend(tenancy.tenant_ids(org))
    return {
        "orgs": list(dict.fromkeys(orgs)),
        "default_org": tenancy.tenant_id(credential.default_org),
    }


def compile_ingest_bindings(tenancy: Tenancy) -> dict[str, Any]:
    """dfe-receiver's ``ingest_bindings`` block: each credential's orgs as tenant ids.

    A credential binding an org the registry no longer holds is left out, so its
    records may land in the default org alone, never in an org it was not given.

    Args:
        tenancy: The orgs and credentials to compile.

    Returns:
        ``{"credentials": {<id>: {"orgs": [...], "default_org": ...}}}``, ids sorted.
    """
    credentials: dict[str, Any] = {}
    for credential in sorted(tenancy.credentials, key=lambda c: c.name):
        try:
            credentials[credential.name] = _binding(tenancy, credential)
        except UnknownOrgError as exc:
            logger.warning(
                "ingest credential left unbound: it names an org that is not registered",
                ingest_id=credential.name,
                org=str(exc),
            )
    return {"credentials": credentials}
