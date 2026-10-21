"""Static service descriptors for DFE services.

ServiceDescriptor names a service. What an app runs, listens on and probes is the
app manifest's (``appmgmt/apps.yaml``), never a descriptor's.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceDescriptor:
    """Immutable metadata about a DFE service.

    This is compile-time data -- not user-editable config.

    Attributes:
        name: Service name (e.g. "receiver", "loader", "transformer").
        display_name: Human-friendly name (e.g. "DFE Receiver").
        description: Short description of the service.
    """

    name: str
    display_name: str
    description: str = ""
