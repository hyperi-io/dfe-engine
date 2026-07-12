#  Project:      dfe-engine
#  File:         kafka/cloud/redpanda.py
#  Purpose:      RedpandaCloudProvider - Redpanda Cloud Serverless lifecycle
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Redpanda Cloud Serverless - the first (proven) ``ManagedKafkaProvider``.

Promoted from the ``.tmp/redpanda_lifecycle.py`` scratch driver (up: ensure
resource group -> create serverless cluster -> wait on the provider operation ->
mint a SCRAM-512 user + ACLs; down: delete the user BEFORE the cluster, then
assert the cluster list is empty), onto scalo's :class:`~scalo.http.HttpClient`
(retries/timeouts/observability - never raw ``urllib``/``httpx``, pylib policy)
behind an injectable ``http_factory`` so tests mock the HTTP layer entirely (the
same shape ``dfe_engine.clickhouse.cloud.CloudService`` uses).

Two hosts are involved - the OAuth2 token endpoint (``auth.prd.cloud.redpanda.
com``) and the control-plane API (``api.redpanda.com``) - plus a THIRD, per-
cluster dataplane host returned by the control plane itself (``dataplane_api.
url``) that mints/revokes the SASL user. None of them share a ``base_url``, so
every call here uses a full URL rather than ``HttpClient(base_url=...)``.
"""

from __future__ import annotations

import secrets as _stdlib_secrets
import string
import time
from collections.abc import Callable
from typing import Any

from scalo.logger import logger

from dfe_engine.settings import RedpandaCloudSettings

from .base import (
    KafkaClusterState,
    KafkaConnection,
    ManagedKafkaProvider,
    ManagedKafkaProviderError,
)

_PASSWORD_ALPHABET = string.ascii_letters + string.digits
_PASSWORD_LENGTH = 32
_OPERATION_POLL_S = 5.0

# The provider ready-signal states (gate on these, never a guessed timer).
_OP_COMPLETED = "STATE_COMPLETED"
_OP_FAILED = "STATE_FAILED"

_ACL_GRANTS: tuple[tuple[str, str], ...] = (
    ("RESOURCE_TYPE_TOPIC", "*"),
    ("RESOURCE_TYPE_GROUP", "*"),
    ("RESOURCE_TYPE_CLUSTER", "kafka-cluster"),
)


class RedpandaCloudProvider(ManagedKafkaProvider):
    """Redpanda Cloud Serverless control-plane driver.

    Args:
        config: the ``kafka.redpanda_cloud`` settings block (OAuth2 client +
            resource group/cluster/user selectors).
        http_factory: OPTIONAL () -> a context manager exposing
            ``get(url, **kw)`` / ``post(url, **kw)`` / ``delete(url, **kw)``
            returning an object with ``.json()`` (the scalo HttpClient shape).
            Defaults to a real :class:`scalo.http.HttpClient`. Tests inject a
            fake recording client so no network is ever touched.
        sleep / now: injectable clock for operation polling (tests pass fakes
            so a stuck-operation backstop never really sleeps).
        wait_backstop_s: hard ceiling on operation polling - a backstop for a
            genuinely-stuck provider operation, never the mechanism raced
            against (gate on the operation's own ``state``, per house style).
    """

    name = "redpanda-cloud"

    def __init__(
        self,
        config: RedpandaCloudSettings,
        *,
        http_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
        wait_backstop_s: float = 600.0,
    ) -> None:
        self._config = config
        self._http_factory = http_factory or self._default_http_factory
        self._sleep = sleep
        self._now = now
        self._wait_backstop_s = wait_backstop_s
        self._token_cache: str | None = None

    def _default_http_factory(self) -> Any:
        # Lazy import (pylib convention) so importing this module never drags httpx.
        from scalo.http import HttpClient

        return HttpClient()

    # -- transport -----------------------------------------------------------

    def _do(
        self,
        method: str,
        url: str,
        *,
        token: str | None = None,
        json_body: dict | None = None,
        form_body: dict | None = None,
    ) -> dict:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        kwargs: dict[str, Any] = {"headers": headers}
        if form_body is not None:
            kwargs["data"] = form_body
        elif json_body is not None:
            kwargs["json"] = json_body
        try:
            with self._http_factory() as client:
                response = getattr(client, method)(url, **kwargs)
                return response.json() or {}
        except ManagedKafkaProviderError:
            raise
        except Exception as exc:
            raise ManagedKafkaProviderError(f"{method.upper()} {url} failed: {exc}") from exc

    def _token(self) -> str:
        if self._token_cache:
            return self._token_cache
        if not self._config.configured:
            raise ManagedKafkaProviderError(
                "Redpanda Cloud is not configured: set DFE_REDPANDA_API_KEY + "
                "DFE_REDPANDA_API_SECRET (the OAuth2 control-plane client)."
            )
        body = self._do(
            "post",
            self._config.auth_url,
            form_body={
                "grant_type": "client_credentials",
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
                "audience": self._config.audience,
            },
        )
        token = body.get("access_token")
        if not token:
            raise ManagedKafkaProviderError(f"OAuth2 token response missing access_token: {body}")
        self._token_cache = token
        return token

    # -- control-plane reads ---------------------------------------------------

    def _ensure_resource_group(self, token: str) -> str:
        """Find-or-create the configured resource group (idempotent)."""
        cfg = self._config
        body = self._do("get", f"{cfg.api_base}/v1/resource-groups", token=token)
        for rg in body.get("resource_groups", []):
            if rg.get("name") == cfg.resource_group:
                return rg["id"]
        body = self._do(
            "post",
            f"{cfg.api_base}/v1/resource-groups",
            token=token,
            json_body={"resource_group": {"name": cfg.resource_group}},
        )
        return body["resource_group"]["id"]

    def _pick_region(self, token: str) -> str:
        """A serverless region - the endpoint refuses CLOUD_PROVIDER_UNSPECIFIED,
        so ask per cloud provider and prefer a US region (nearest to the rest of
        DFE's dev footprint)."""
        cfg = self._config
        regions: list[dict] = []
        for provider in ("CLOUD_PROVIDER_AWS", "CLOUD_PROVIDER_GCP"):
            body = self._do(
                "get",
                f"{cfg.api_base}/v1/serverless/regions?cloud_provider={provider}",
                token=token,
            )
            regions.extend(body.get("serverless_regions", []))
        if not regions:
            raise ManagedKafkaProviderError("no Redpanda Cloud serverless regions returned")
        for region in regions:
            if "us-" in region.get("name", ""):
                return region["name"]
        return regions[0]["name"]

    def _find_cluster(self, token: str) -> dict | None:
        cfg = self._config
        body = self._do("get", f"{cfg.api_base}/v1/serverless/clusters", token=token)
        for cluster in body.get("serverless_clusters", []):
            if cluster.get("name") == cfg.cluster_name:
                return cluster
        return None

    def _get_cluster(self, token: str, cluster_id: str) -> dict:
        cfg = self._config
        body = self._do("get", f"{cfg.api_base}/v1/serverless/clusters/{cluster_id}", token=token)
        return body.get("serverless_cluster") or body.get("cluster") or body

    @staticmethod
    def _conn_from_cluster(cluster: dict) -> tuple[str, str]:
        """(dataplane_api_url, bootstrap_seed_broker) from a cluster body."""
        dataplane = cluster.get("dataplane_api", {}).get("url", "")
        seeds = cluster.get("kafka_api", {}).get("seed_brokers") or [""]
        return dataplane, seeds[0]

    def _wait_operation(self, token: str, operation_id: str) -> None:
        """Gate on the provider's own operation state; backstop only for stuck."""
        deadline = self._now() + self._wait_backstop_s
        while True:
            body = self._do(
                "get", f"{self._config.api_base}/v1/operations/{operation_id}", token=token
            )
            state = body.get("operation", body).get("state", "")
            if state == _OP_COMPLETED:
                return
            if state == _OP_FAILED:
                raise ManagedKafkaProviderError(f"operation {operation_id} FAILED: {body}")
            if self._now() >= deadline:
                raise ManagedKafkaProviderError(
                    f"operation {operation_id} stuck past {self._wait_backstop_s:.0f}s backstop"
                )
            self._sleep(_OPERATION_POLL_S)

    def _ensure_acls(self, token: str, dataplane: str, user: str) -> None:
        """Grant all-topic/all-group/cluster ACLs to ``user``. Best-effort: a
        single ACL failure must not abort ``up`` (mirrors the proven scratch
        driver, which WARNs on the topic ACL and does not even check the
        group/cluster ACL calls)."""
        for resource_type, resource_name in _ACL_GRANTS:
            try:
                self._do(
                    "post",
                    f"{dataplane}/v1/acls",
                    token=token,
                    json_body={
                        "resource_type": resource_type,
                        "resource_name": resource_name,
                        "resource_pattern_type": "RESOURCE_PATTERN_TYPE_LITERAL",
                        "principal": f"User:{user}",
                        "host": "*",
                        "operation": "OPERATION_ALL",
                        "permission_type": "PERMISSION_TYPE_ALLOW",
                    },
                )
            except ManagedKafkaProviderError as exc:
                logger.warning(
                    "Redpanda Cloud ACL grant failed (best-effort)",
                    resource_type=resource_type,
                    error=str(exc),
                )

    # -- ManagedKafkaProvider --------------------------------------------------

    def up(self) -> KafkaConnection:
        from dfe_engine.kafka import contract

        cfg = self._config
        token = self._token()
        cluster = self._find_cluster(token)
        if cluster is None:
            resource_group_id = self._ensure_resource_group(token)
            region = self._pick_region(token)
            logger.info(
                "Creating Redpanda Cloud serverless cluster (billable)",
                cluster_name=cfg.cluster_name,
                resource_group=cfg.resource_group,
                region=region,
            )
            body = self._do(
                "post",
                f"{cfg.api_base}/v1/serverless/clusters",
                token=token,
                json_body={
                    "serverless_cluster": {
                        "name": cfg.cluster_name,
                        "resource_group_id": resource_group_id,
                        "serverless_region": region,
                    }
                },
            )
            operation = body.get("operation", {})
            if operation.get("id"):
                self._wait_operation(token, operation["id"])
            cluster = self._find_cluster(token)
            if cluster is None:
                raise ManagedKafkaProviderError(
                    f"cluster {cfg.cluster_name!r} not found after create + wait"
                )

        dataplane, seed = self._conn_from_cluster(self._get_cluster(token, cluster["id"]))
        password = "".join(
            _stdlib_secrets.choice(_PASSWORD_ALPHABET) for _ in range(_PASSWORD_LENGTH)
        )
        # Mint the data-plane user BEFORE granting ACLs (an ACL for a user that
        # does not exist yet is meaningless) - create THEN acl, never the reverse.
        self._do(
            "post",
            f"{dataplane}/v1/users",
            token=token,
            json_body={
                "name": cfg.kafka_user,
                "password": password,
                "mechanism": "SASL_MECHANISM_SCRAM_SHA_512",
            },
        )
        self._ensure_acls(token, dataplane, cfg.kafka_user)

        # Mechanism DERIVED from the contract (dfe-engine#98) - never hand-set.
        protocol, mechanism = contract.derive(self.name)
        return KafkaConnection(
            cluster_id=cluster["id"],
            bootstrap_servers=seed,
            security_protocol=protocol,
            sasl_mechanism=mechanism,
            username=cfg.kafka_user,
            password=password,
            extra={"http_endpoint": dataplane, "resource_group": cfg.resource_group},
        )

    def down(self) -> KafkaClusterState:
        cfg = self._config
        token = self._token()
        cluster = self._find_cluster(token)
        if cluster is None:
            return KafkaClusterState(exists=False)

        dataplane, _ = self._conn_from_cluster(self._get_cluster(token, cluster["id"]))
        # Credentials die BEFORE the cluster - deleting the cluster first orphans
        # the key (the Confluent 403-orphan lesson; docs/MANAGED-KAFKA-LIFECYCLE.md).
        # Best-effort: the user may already be gone from a prior partial teardown.
        try:
            self._do("delete", f"{dataplane}/v1/users/{cfg.kafka_user}", token=token)
        except ManagedKafkaProviderError as exc:
            logger.warning("Redpanda Cloud user delete failed (best-effort)", error=str(exc))

        body = self._do(
            "delete", f"{cfg.api_base}/v1/serverless/clusters/{cluster['id']}", token=token
        )
        operation = body.get("operation", {})
        if operation.get("id"):
            self._wait_operation(token, operation["id"])

        left = self._find_cluster(token)
        if left is None:
            return KafkaClusterState(exists=False)
        return KafkaClusterState(
            exists=True, cluster_id=left.get("id", ""), state=left.get("state", "")
        )

    def status(self) -> KafkaClusterState:
        token = self._token()
        cluster = self._find_cluster(token)
        if cluster is None:
            return KafkaClusterState(exists=False)
        full = self._get_cluster(token, cluster["id"])
        dataplane, seed = self._conn_from_cluster(full)
        return KafkaClusterState(
            exists=True,
            cluster_id=cluster.get("id", ""),
            state=full.get("state", ""),
            bootstrap_servers=seed,
            extra={"dataplane": dataplane},
        )
