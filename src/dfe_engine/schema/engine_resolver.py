"""Single source of truth for ClickHouse table-engine selection.

Every table dfe-engine creates - source/profile data tables AND the engine's own
internal/meta tables (audit checkpoints, alert state, hunt-runner coordination,
RBAC materialisation) - resolves its storage engine through THIS module. No table
DDL hardcodes an engine string.

Why a resolver at all: the *most capable* engine differs per ClickHouse topology
and you cannot tell which from the DDL alone (live-proven 2026-07-06):
  - keeperless single node      -> plain MergeTree (Replicated forms are REJECTED)
  - on-prem Replicated database -> ReplicatedMergeTree (data replicates)
  - on-prem Atomic db + cluster -> ReplicatedMergeTree ON CLUSTER
  - ClickHouse Cloud            -> SharedMergeTree (server auto-substitutes)
Sending the wrong one either hard-fails (code 36) or silently splits data across
replicas. The path/replica are NEVER emitted in the DDL - they are the server's
job (default_replica_path/default_replica_name macros), i.e. an infra concern.

Resolution is a CASCADE, first match wins (a hardcoded default is fine ONLY as
the terminal fallback):
  1. explicit override  - a per-source/table engine pin from config
  2. live sense         - introspect the target server (needs a client)
  3. deployment setting - DFE_CLICKHOUSE_TOPOLOGY (static/gitops path, no client)
  4. terminal default   - "single" -> plain <variant>() (safe everywhere; Cloud
                          still auto-substitutes Shared)

The MergeTree VARIANT and its params are always preserved:
ReplacingMergeTree(ver) -> ReplicatedReplacingMergeTree(ver) on a cluster, and
CH Cloud maps that to SharedReplacingMergeTree.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from scalo.logger import logger


@dataclass(frozen=True)
class EngineSpec:
    """The engine INTENT a call site declares - what the table needs, not how the
    topology renders it.

    variant: the MergeTree-family variant, e.g. "MergeTree", "ReplacingMergeTree".
    params:  the args inside the engine parens, e.g. "last_fired_at" for a
             ReplacingMergeTree version column. Empty string for none.
    """

    variant: str = "MergeTree"
    params: str = ""


def parse_engine(engine: str) -> EngineSpec:
    """Split an engine string into an ``EngineSpec`` (variant + params).

    Accepts both the bare variant and the parameterised form, so any
    MergeTree-family engine - not just plain MergeTree - flows through the same
    resolver:

      "MergeTree"                          -> EngineSpec("MergeTree", "")
      "ReplacingMergeTree(last_fired_at)"  -> EngineSpec("ReplacingMergeTree", "last_fired_at")
      "SummingMergeTree(a, b)"             -> EngineSpec("SummingMergeTree", "a, b")

    A trailing empty ``()`` is treated as no params. Whitespace-tolerant.
    """
    s = engine.strip()
    if "(" not in s:
        return EngineSpec(variant=s, params="")
    variant, _, rest = s.partition("(")
    params = rest.rsplit(")", 1)[0].strip()
    return EngineSpec(variant=variant.strip(), params=params)


class Topology(str, Enum):
    """The resolved deployment shape that dictates the engine form."""

    SINGLE = "single"  # standalone/keeperless -> plain <variant>()
    REPLICATED = "replicated"  # Replicated/Shared db or Cloud -> argumentless Replicated<variant>, no ON CLUSTER
    REPLICATED_ON_CLUSTER = (
        "replicated_on_cluster"  # Atomic db on a real cluster -> Replicated<variant> ON CLUSTER
    )


@dataclass(frozen=True)
class ResolvedEngine:
    """The concrete result a call site splices into its CREATE TABLE.

    clause:     the full token after ``ENGINE =`` (e.g. "ReplicatedReplacingMergeTree(ver)").
    on_cluster: " ON CLUSTER <name>" to insert after the table name, or "".
    topology:   "single" | "replicated" - for feeding DDLConfig.topology on the
                shared generator path.
    origin:     which cascade layer decided (for logging/debuggability).
    """

    clause: str
    on_cluster: str
    topology: str
    origin: str


def _engine_clause(spec: EngineSpec, topology: Topology) -> str:
    """Render the ENGINE clause for a variant+params under a topology.

    plain:       <variant>(<params>)            e.g. MergeTree()  / ReplacingMergeTree(ver)
    replicated:  Replicated<variant>            (argumentless - server supplies path/replica)
                 Replicated<variant>(<params>)  when the variant needs params (e.g. a ver col)
    """
    if topology is Topology.SINGLE:
        return f"{spec.variant}({spec.params})"
    # replicated / replicated_on_cluster: argumentless base; keep variant params if any.
    if spec.params:
        return f"Replicated{spec.variant}({spec.params})"
    return f"Replicated{spec.variant}"


class EngineResolver:
    """Resolves a table's ClickHouse engine via the config cascade.

    Construct once per apply pass with whatever cascade inputs are available:
    a live ``client`` enables sensing; ``override`` / ``topology_setting`` feed the
    other layers. Sensing results are cached per database.
    """

    def __init__(
        self,
        client=None,
        *,
        override: str | None = None,
        topology_setting: str | None = None,
    ) -> None:
        """
        Args:
            client: a live clickhouse_connect client (exposes ``.query``), or None.
                    Presence enables the live-sense cascade layer.
            override: an explicit per-source/table topology pin ("single" |
                    "replicated"), or None. Highest-priority cascade layer.
            topology_setting: the deployment DFE_CLICKHOUSE_TOPOLOGY value, or None.
                    Used when there is no client to sense.
        """
        self._client = client
        self._override = override
        self._topology_setting = topology_setting
        self._sensed: dict[str, Topology] = {}

    # -- public API --------------------------------------------------

    def resolve(self, spec: EngineSpec, database: str) -> ResolvedEngine:
        """Resolve ``spec`` for ``database`` down the cascade."""
        topology, origin = self._cascade(database)
        on_cluster = ""
        if topology is Topology.REPLICATED_ON_CLUSTER:
            cluster = self._cluster_name(database)
            on_cluster = f" ON CLUSTER {cluster}" if cluster else ""
        ddl_topology = "single" if topology is Topology.SINGLE else "replicated"
        clause = _engine_clause(spec, topology)
        logger.debug(
            f"engine resolved: db={database} variant={spec.variant} "
            f"-> {clause}{on_cluster} (topology={topology.value}, via {origin})"
        )
        return ResolvedEngine(
            clause=clause, on_cluster=on_cluster, topology=ddl_topology, origin=origin
        )

    # -- cascade -----------------------------------------------------

    def _cascade(self, database: str) -> tuple[Topology, str]:
        # 1. explicit config override
        if self._override:
            return self._topology_from_name(self._override), "config-override"
        # 2. live sense
        if self._client is not None:
            sensed = self._sense(database)
            if sensed is not None:
                return sensed, "sensed"
        # 3. deployment setting
        if self._topology_setting:
            return self._topology_from_name(self._topology_setting), "topology-setting"
        # 4. terminal hardcoded default (safe everywhere; Cloud auto-upgrades)
        return Topology.SINGLE, "default"

    @staticmethod
    def _topology_from_name(name: str) -> Topology:
        # A named topology ("single"/"replicated") from config carries no
        # ON CLUSTER intent - that only comes from sensing an Atomic cluster.
        return Topology.SINGLE if name.strip().lower() == "single" else Topology.REPLICATED

    # -- sensing (live introspection) --------------------------------

    def _sense(self, database: str) -> Topology | None:
        """Introspect the live server and classify the topology. Cached per db.

        Returns None if sensing itself fails (falls through to the next layer).
        """
        if database in self._sensed:
            return self._sensed[database]
        try:
            # CH Cloud: any MergeTree-family CREATE auto-substitutes SharedMergeTree.
            if self._scalar("SELECT value FROM system.settings WHERE name = 'cloud_mode'") == "1":
                return self._cache(database, Topology.REPLICATED)

            # A Replicated/Shared target database propagates + replicates on its own;
            # argumentless Replicated<engine> is correct there with NO ON CLUSTER.
            db_engine = self._scalar(
                f"SELECT engine FROM system.databases WHERE name = '{database}'"
            )
            if db_engine in ("Replicated", "Shared"):
                return self._cache(database, Topology.REPLICATED)

            # Plain Atomic/Ordinary db: replication is only possible if the server is
            # cluster-configured (shard+replica macros). Then we must emit ON CLUSTER.
            macros = {row[0] for row in self._rows("SELECT macro FROM system.macros")}
            if {"shard", "replica"} <= macros:
                return self._cache(database, Topology.REPLICATED_ON_CLUSTER)

            # Standalone/keeperless: plain <engine>() is the ONLY form that works.
            return self._cache(database, Topology.SINGLE)
        except Exception as e:
            logger.warning(f"engine sensing failed for db={database}: {e}; falling through cascade")
            return None

    def _cluster_name(self, database: str) -> str | None:
        """Pick a cluster to fan the DDL over. Prefers the macro-declared cluster,
        else the first non-single-host cluster in system.clusters."""
        try:
            macro_cluster = self._scalar(
                "SELECT substitution FROM system.macros WHERE macro = 'cluster'"
            )
            if macro_cluster:
                return macro_cluster
            rows = self._rows(
                "SELECT cluster FROM system.clusters GROUP BY cluster "
                "HAVING count() > 1 ORDER BY cluster LIMIT 1"
            )
            return rows[0][0] if rows else None
        except Exception as e:
            logger.warning(f"cluster-name sensing failed for db={database}: {e}")
            return None

    # -- client helpers ----------------------------------------------

    def _cache(self, database: str, topology: Topology) -> Topology:
        self._sensed[database] = topology
        return topology

    def _rows(self, sql: str) -> list:
        """Run a query, returning result rows (empty list on no rows)."""
        return self._client.query(sql).result_rows

    def _scalar(self, sql: str) -> str | None:
        """Run a query, returning the first column of the first row as str, or None."""
        rows = self._rows(sql)
        if not rows:
            return None
        value = rows[0][0]
        return str(value) if value is not None else None
