#  Project:      dfe-engine
#  File:         kafka/contract.py
#  Purpose:      DFE Kafka credential contract - derive sasl.mechanism from the
#                provider type (never hand-set). Canonical record: dfe-engine#98.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One credential contract: SASL, username+password, mechanism DERIVED from the
provider type.

The old "SCRAM-512 everywhere" single-mechanism standard is dead - Confluent Cloud
has no SCRAM. The invariant that DOES hold everywhere is SASL with a
username+password pair; only the ``sasl.mechanism`` string differs, and it is
derivable from the provider. Client code is written once against the user/pass
contract. TLS under it is the default and the only exception is the in-cluster
listener below.

SSoT is moving into scalo: the generic FACTS live in ``scalo.kafka.providers`` and
the opt-in strict profile (blessed set + no-weakening) in ``scalo.kafka.contract``.
This module is a TEMPORARY mirror while dfe-engine is pinned to a published scalo-py
that predates those modules. Once the scalo pin bumps to the version carrying them,
COLLAPSE this to ``from scalo.kafka.providers import derive, validate`` (and
``scalo.kafka.contract`` for the DFE opinion) and delete the duplicated table. It is
also MIRRORED in scalo-rs (Rust). The dfe-engine#98 spec stays the arbiter; every
copy has a canonical-table test that fails if the tables diverge.

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
whole input every layer derives from: scalo parses one string, so a second input
would have to be added to all three tables. SCRAM-SHA-512 is the one mechanism
allowed onto a plaintext transport; there is deliberately no such key for a PLAIN
or OAUTHBEARER provider.
"""

from __future__ import annotations

# (security_protocol, sasl_mechanism) pairs.
_SCRAM = ("SASL_SSL", "SCRAM-SHA-512")
_SCRAM_NO_TLS = ("SASL_PLAINTEXT", "SCRAM-SHA-512")
_PLAIN = ("SASL_SSL", "PLAIN")

# Local dev with no auth.
PLAINTEXT = "plaintext"
# Quarantined: MSK on IAM auth (OAUTHBEARER token via the AWS MSK IAM callback -- a
# different credential shape AND client-lib requirement, not username+password).
# MANDATORY for MSK Serverless (IAM is its ONLY auth); optional on provisioned MSK.
# Handled at the mode=external seam, NOT on the username+password contract.
MSK_IAM = "msk_iam"

# provider -> (security_protocol, sasl_mechanism). The username+password contract.
DERIVATION: dict[str, tuple[str, str]] = {
    # DFE-owned brokers -- SCRAM-512 mandatory, never weakened.
    "strimzi": _SCRAM,
    "redpanda": _SCRAM,
    # The same two brokers on the TLS-off listener the deploy charts stand up
    # in-cluster (dfe-engine#332, dfe-infra#191); SCRAM is a challenge-response, so
    # nothing secret crosses that transport.
    "strimzi-no-tls": _SCRAM_NO_TLS,
    "redpanda-no-tls": _SCRAM_NO_TLS,
    # Provisioned MSK -- SASL/SCRAM-512 (+ AWS Secrets Manager). PROVISIONED ONLY:
    # MSK Serverless is IAM-only (no SASL/SCRAM, verified 2026-07) -> use msk_iam.
    "msk": _SCRAM,
    # Redpanda Cloud -- SASL_SSL + SCRAM-SHA-256/512 (verified 2026-07);
    # a SCRAM provider like the DFE-owned brokers, not a PLAIN exception.
    "redpanda-cloud": _SCRAM,
    # Confluent Cloud -- SCRAM forbidden by the platform -> API-key PLAIN over TLS.
    # Uniform across ALL tiers (Basic/Standard/Enterprise/Freight/Dedicated); tier
    # changes cost + always-on profile, not the mechanism. (OAuth is Standard+ but
    # out of scope here.)
    "confluent-cloud": _PLAIN,
}

# Everything the contract knows how to derive (for error messages + validation).
KNOWN_PROVIDERS = sorted(DERIVATION) + [PLAINTEXT, MSK_IAM]

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
    if provider == PLAINTEXT:
        return ("PLAINTEXT", "")
    if provider == MSK_IAM:
        return ("SASL_SSL", "OAUTHBEARER")
    try:
        return DERIVATION[provider]
    except KeyError:
        raise KafkaContractError(
            f"unknown kafka provider {provider!r}; expected one of {KNOWN_PROVIDERS}"
        ) from None


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
