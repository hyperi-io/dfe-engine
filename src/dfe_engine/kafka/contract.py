#  Project:      dfe-engine
#  File:         kafka/contract.py
#  Purpose:      DFE Kafka credential contract - derive sasl.mechanism from the
#                provider type (never hand-set). Canonical record: dfe-engine#98.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One credential contract: SASL with username+password, the mechanism DERIVED from the provider.

The old "SCRAM-512 everywhere" single-mechanism standard is dead - Confluent Cloud
has no SCRAM. The invariant that DOES hold everywhere is SASL with a
username+password pair; only the ``sasl.mechanism`` string differs, and it is
derivable from the provider. Client code is written once against the user/pass
contract. TLS under it is the default and the only exception is the in-cluster
listener below.

The provider table is scalo's (``scalo.kafka.providers``), also carried by scalo-rs.
This module adds the DFE error type and a stricter floor than scalo's ``validate``:
OAUTHBEARER, like PLAIN, never rides a plaintext transport.

Rules (dfe-engine#98):
- SCRAM-SHA-512 wherever DFE controls the broker (Strimzi, Redpanda, provisioned
  MSK). Never weakened to PLAIN.
- PLAIN is the single sanctioned fallback, ONLY where the platform forbids SCRAM
  (Confluent Cloud). Always over TLS, never plaintext transport.
- msk_iam stays quarantined at the mode=external seam (a different credential
  shape, not username+password); MANDATORY for MSK Serverless (IAM-only).
- OAUTHBEARER/OIDC is the end-state watch item, OUT of scope here.

Amended by dfe-engine#332 with ``strimzi-no-tls`` and ``redpanda-no-tls``: one
provider key names one LISTENER, not a cluster. The Strimzi and Redpanda the
deploy charts stand up run SASL on a TLS-off listener and point every app at it,
so ``strimzi`` - which means SASL_SSL - was the only name for a broker that
speaks SASL_PLAINTEXT, and every admin call against it failed with a broker
transport failure. A key rather than a flag because the provider identity is the
whole input every layer derives from. SCRAM-SHA-512 is the one mechanism
allowed onto a plaintext transport; there is deliberately no such key for a PLAIN
or OAUTHBEARER provider.
"""

from scalo.kafka.providers import (
    DERIVATION,
    KNOWN_PROVIDERS,
    MSK_IAM,
    PLAINTEXT,
    KafkaProviderError,
)
from scalo.kafka.providers import derive as _derive

__all__ = [
    "DERIVATION",
    "KNOWN_PROVIDERS",
    "MSK_IAM",
    "ON_THE_WIRE",
    "PLAINTEXT",
    "KafkaContractError",
    "derive",
    "validate",
]

# Mechanisms that put the secret itself on the wire, so nothing under them but TLS
# will do; SCRAM is absent because its challenge-response never sends the password.
ON_THE_WIRE = frozenset({"PLAIN", "OAUTHBEARER"})


class KafkaContractError(ValueError):
    """A Kafka credential configuration that violates the contract."""


def derive(provider: str) -> tuple[str, str]:
    """Return ``(security_protocol, sasl_mechanism)`` for a provider.

    ``plaintext``        -> ``("PLAINTEXT", "")`` (local dev, no auth).
    ``*-no-tls``         -> ``("SASL_PLAINTEXT", "SCRAM-SHA-512")`` (a DFE-owned
                            broker on its TLS-off in-cluster listener).
    ``msk_iam``          -> ``("SASL_SSL", "OAUTHBEARER")`` (quarantined IAM token
                            path; the caller wires the AWS MSK IAM callback).
    unknown              -> ``KafkaContractError``.
    """
    try:
        return _derive(provider)
    except KafkaProviderError as exc:
        raise KafkaContractError(str(exc)) from None


def validate(*, security_protocol: str, sasl_mechanism: str) -> None:
    """Refuse contract violations. Raises ``KafkaContractError``.

    The one hard floor: a mechanism that sends the credential itself MUST ride
    SASL_SSL, never a plaintext transport. SASL_SSL requires a mechanism.
    """
    if sasl_mechanism in ON_THE_WIRE and security_protocol != "SASL_SSL":
        raise KafkaContractError(
            f"{sasl_mechanism} credentials require security_protocol=SASL_SSL "
            "(never send the credential itself over a plaintext transport)"
        )
    if security_protocol == "SASL_SSL" and not sasl_mechanism:
        raise KafkaContractError("security_protocol=SASL_SSL requires a sasl.mechanism")
