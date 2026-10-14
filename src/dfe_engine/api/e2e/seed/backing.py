#  Project:      dfe-engine
#  File:         api/e2e/seed/backing.py
#  Purpose:      Backing-service facts an e2e spec renders managed cards from
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Backing-service facts, seeded where the deployer's environment would put them.

Who runs Kafka and ClickHouse is a deployment fact, never deploy-repo state, so
the seed sets the process settings and writes nothing to the deploy repo. A reset
puts back what the environment states.
"""

from dfe_engine.api.e2e.seed.base import Seed
from dfe_engine.settings import BackingFactSettings, BackingSettings, backing_from_environment

SEED_KAFKA_PROVIDER = "confluent-cloud"
"""A provider-managed Kafka: the card shows the provider, tier and region, sizing locked."""

SEED_KAFKA_TIER = "basic"
SEED_KAFKA_REGION = "us-east-1"

SEED_CLICKHOUSE_PROVIDER = "external"
"""An externally run ClickHouse: the card shows facts only."""


class Backing(Seed):
    """Make the backing services read as managed, the way a deployer's facts would."""

    def seed_managed_backing(self) -> dict[str, str]:
        """State Kafka as provider-managed and ClickHouse as externally run.

        Returns:
            Each service's stated provider key.

        Raises:
            RuntimeError: The seeder was built without the engine settings.
        """
        settings = self._require_settings()
        settings.deployment.backing = BackingSettings(
            kafka=BackingFactSettings(
                provider=SEED_KAFKA_PROVIDER, tier=SEED_KAFKA_TIER, region=SEED_KAFKA_REGION
            ),
            clickhouse=BackingFactSettings(provider=SEED_CLICKHOUSE_PROVIDER),
        )
        return {"kafka": SEED_KAFKA_PROVIDER, "clickhouse": SEED_CLICKHOUSE_PROVIDER}

    def reset_backing(self) -> None:
        """Put the facts back to what the environment states. A no-op without settings."""
        if self._settings is not None:
            self._settings.deployment.backing = backing_from_environment()
