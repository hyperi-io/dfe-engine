#  Project:      dfe-engine
#  File:         src/dfe_engine/query/datasources/storage.py
#  Purpose:      Storage datasource adapters (S3, MinIO, filesystem)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Storage datasource adapters for directory listing.

Returns rows as list[dict] with consistent column schema across backends.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from hyperi_pylib.logger import logger

from dfe_engine.query.datasources import DatasourceAdapter, register_adapter
from dfe_engine.query.models import ExplainPlan, ExplainStep, ExplainStepType

LISTING_COLUMNS = [
    "name",
    "path",
    "type",
    "size",
    "modified",
    "etag",
    "storage_class",
    "content_type",
]


class StorageListingError(Exception):
    """Error during storage listing."""


@register_adapter("s3")
class S3Adapter(DatasourceAdapter):
    """S3/MinIO datasource adapter for directory listing."""

    def __init__(self, target: str, config: dict[str, Any] | None = None):
        super().__init__(target, config)
        self._client = None

    @property
    def client(self):
        """Lazy-load S3 client."""
        if self._client is None:
            import boto3
            from botocore.config import Config

            endpoint_url = self.config.get("endpoint_url")
            access_key = self.config.get("access_key_id")
            secret_key = self.config.get("secret_access_key")
            region = self.config.get("region", "us-east-1")

            if not endpoint_url:
                from dfe_engine.settings import get_settings

                settings = get_settings()
                endpoint_url = getattr(settings, "s3_endpoint_url", None)
                access_key = access_key or getattr(settings, "s3_access_key_id", None)
                secret_key = secret_key or getattr(settings, "s3_secret_access_key", None)
                region = getattr(settings, "s3_region", region)

            client_config = Config(
                retries={"max_attempts": 3, "mode": "adaptive"},
                connect_timeout=5,
                read_timeout=30,
            )

            self._client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region,
                config=client_config,
            )
        return self._client

    def execute(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: int = 30,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """List objects in S3 bucket. Returns (rows, columns)."""
        params = params or {}

        bucket = params.get("bucket") or self.target
        if bucket == "default":
            bucket = self.config.get("bucket") or params.get("bucket")
            if not bucket:
                raise StorageListingError("No bucket specified")

        prefix = params.get("prefix", query.strip() if query else "")
        delimiter = params.get("delimiter", "/")
        max_keys = min(params.get("limit", 1000), 10000)

        list_params: dict[str, Any] = {"Bucket": bucket, "MaxKeys": max_keys}
        if prefix:
            list_params["Prefix"] = prefix
        if delimiter:
            list_params["Delimiter"] = delimiter
        if params.get("_cursor"):
            list_params["ContinuationToken"] = params["_cursor"]

        try:
            response = self.client.list_objects_v2(**list_params)
        except Exception as e:
            logger.error("S3 listing failed", bucket=bucket, prefix=prefix, error=str(e))
            raise StorageListingError(f"Failed to list S3 bucket: {e}") from e

        rows: list[dict[str, Any]] = []

        for prefix_obj in response.get("CommonPrefixes", []):
            prefix_path = prefix_obj["Prefix"]
            rows.append(
                {
                    "name": prefix_path.rstrip("/").split("/")[-1],
                    "path": prefix_path,
                    "type": "directory",
                    "size": 0,
                    "modified": None,
                    "etag": None,
                    "storage_class": None,
                    "content_type": None,
                }
            )

        for obj in response.get("Contents", []):
            key = obj["Key"]
            if key == prefix:
                continue
            rows.append(
                {
                    "name": key.split("/")[-1],
                    "path": key,
                    "type": "file",
                    "size": obj.get("Size", 0),
                    "modified": obj.get("LastModified"),
                    "etag": obj.get("ETag", "").strip('"'),
                    "storage_class": obj.get("StorageClass"),
                    "content_type": None,
                }
            )

        return rows, LISTING_COLUMNS

    def explain(self, query: str, params: dict[str, Any] | None = None) -> ExplainPlan:
        params = params or {}
        bucket = params.get("bucket") or self.target
        prefix = params.get("prefix", query.strip() if query else "")
        return ExplainPlan(
            steps=[
                ExplainStep(
                    step_type=ExplainStepType.READ,
                    description=f"List S3 objects: s3://{bucket}/{prefix}",
                    details={"bucket": bucket, "prefix": prefix},
                )
            ],
            warnings=[],
        )

    def healthcheck(self) -> bool:
        try:
            bucket = self.config.get("bucket") or self.target
            if bucket and bucket != "default":
                self.client.head_bucket(Bucket=bucket)
            else:
                self.client.list_buckets()
            return True
        except Exception as e:
            logger.warning("S3 healthcheck failed", error=str(e))
            return False


@register_adapter("minio")
class MinIOAdapter(S3Adapter):
    """MinIO datasource adapter (S3-compatible)."""


@register_adapter("file")
class FilesystemAdapter(DatasourceAdapter):
    """Local filesystem datasource adapter for directory listing."""

    def __init__(self, target: str, config: dict[str, Any] | None = None):
        super().__init__(target, config)
        self._base_path: Path | None = None

    @property
    def base_path(self) -> Path:
        if self._base_path is None:
            path = self.config.get("base_path")
            if not path:
                from dfe_engine.settings import get_settings

                settings = get_settings()
                path = getattr(settings, "file_storage_base_path", None)

            if not path:
                raise StorageListingError("No base_path configured for filesystem adapter")

            self._base_path = Path(path).resolve()
            if not self._base_path.exists():
                raise StorageListingError(f"Base path does not exist: {self._base_path}")

        return self._base_path

    def _resolve_safe_path(self, subpath: str) -> Path:
        clean_subpath = subpath.lstrip("/")
        target_path = (self.base_path / clean_subpath).resolve()
        try:
            target_path.relative_to(self.base_path)
        except ValueError:
            raise StorageListingError(f"Path traversal detected: {subpath}")
        return target_path

    def execute(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: int = 30,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """List files in directory. Returns (rows, columns)."""
        params = params or {}

        subpath = params.get("path", query.strip() if query else "")
        target_path = self._resolve_safe_path(subpath)

        if not target_path.exists():
            raise StorageListingError(f"Path does not exist: {subpath}")
        if not target_path.is_dir():
            raise StorageListingError(f"Path is not a directory: {subpath}")

        recursive = params.get("recursive", False)
        pattern = params.get("pattern", "*")
        limit = params.get("limit", 1000)
        offset = params.get("offset", 0)

        rows: list[dict[str, Any]] = []

        try:
            entries = list(target_path.rglob(pattern) if recursive else target_path.glob(pattern))
            entries = sorted(entries, key=lambda p: (p.is_file(), p.name))
            entries = entries[offset : offset + limit]

            for entry in entries:
                stat = entry.stat()
                rel_path = entry.relative_to(self.base_path)
                rows.append(
                    {
                        "name": entry.name,
                        "path": str(rel_path),
                        "type": "file" if entry.is_file() else "directory",
                        "size": stat.st_size if entry.is_file() else 0,
                        "modified": datetime.fromtimestamp(stat.st_mtime),
                        "etag": None,
                        "storage_class": None,
                        "content_type": self._guess_content_type(entry)
                        if entry.is_file()
                        else None,
                    }
                )
        except PermissionError as e:
            raise StorageListingError(f"Permission denied: {subpath}") from e
        except Exception as e:
            logger.error("Filesystem listing failed", path=subpath, error=str(e))
            raise StorageListingError(f"Failed to list directory: {e}") from e

        return rows, LISTING_COLUMNS

    def _guess_content_type(self, path: Path) -> str | None:
        import mimetypes

        mime_type, _ = mimetypes.guess_type(str(path))
        return mime_type

    def explain(self, query: str, params: dict[str, Any] | None = None) -> ExplainPlan:
        params = params or {}
        subpath = params.get("path", query.strip() if query else "")
        recursive = params.get("recursive", False)
        return ExplainPlan(
            steps=[
                ExplainStep(
                    step_type=ExplainStepType.READ,
                    description=f"List filesystem: {self.base_path / subpath}",
                    details={
                        "base_path": str(self.base_path),
                        "subpath": subpath,
                        "recursive": recursive,
                    },
                )
            ],
            warnings=[],
        )

    def healthcheck(self) -> bool:
        try:
            return self.base_path.exists() and self.base_path.is_dir()
        except Exception as e:
            logger.warning("Filesystem healthcheck failed", error=str(e))
            return False
