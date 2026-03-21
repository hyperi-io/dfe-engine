"""
Storage datasource adapter for directory listing.

Supports S3, MinIO, and local filesystem directory listing as queries.
Returns Arrow format with consistent schema across storage backends.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
from hyperi_pylib.logger import logger

from dfe_engine.query.datasources import DatasourceAdapter, register_adapter
from dfe_engine.query.models import ExplainPlan, ExplainStep, ExplainStepType

# Arrow schema for directory listings
LISTING_SCHEMA = pa.schema(
    [
        pa.field("name", pa.string()),
        pa.field("path", pa.string()),
        pa.field("type", pa.string()),  # "file" or "directory"
        pa.field("size", pa.int64()),
        pa.field("modified", pa.timestamp("us", tz="UTC")),
        pa.field("etag", pa.string()),
        pa.field("storage_class", pa.string()),
        pa.field("content_type", pa.string()),
    ]
)


class StorageListingError(Exception):
    """Error during storage listing."""



@register_adapter("s3")
class S3Adapter(DatasourceAdapter):
    """
    S3/MinIO datasource adapter for directory listing.

    Target format: bucket name or 'default' for config-based bucket.

    Query format (as prefix filter):
        SELECT * FROM 's3://bucket/prefix/'
        Or just use params: {"prefix": "path/to/"}
    """

    def __init__(self, target: str, config: dict[str, Any] | None = None):
        super().__init__(target, config)
        self._client = None

    @property
    def client(self):
        """Lazy-load S3 client."""
        if self._client is None:
            import boto3
            from botocore.config import Config

            # Get configuration from settings or config override
            endpoint_url = self.config.get("endpoint_url")
            access_key = self.config.get("access_key_id")
            secret_key = self.config.get("secret_access_key")
            region = self.config.get("region", "us-east-1")

            # If no explicit config, try environment
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
    ) -> pa.Table:
        """
        List objects in S3 bucket.

        Query can be:
        - A prefix filter string
        - Empty for root listing

        Params:
        - bucket: Bucket name (or use target)
        - prefix: Path prefix to filter
        - delimiter: Path delimiter (default: '/')
        - max_keys: Maximum objects to return
        - continuation_token: For pagination
        """
        params = params or {}

        # Determine bucket
        bucket = params.get("bucket") or self.target
        if bucket == "default":
            bucket = self.config.get("bucket") or params.get("bucket")
            if not bucket:
                raise StorageListingError("No bucket specified")

        # Build list_objects_v2 parameters
        prefix = params.get("prefix", query.strip() if query else "")
        delimiter = params.get("delimiter", "/")
        max_keys = min(params.get("limit", 1000), 10000)

        list_params: dict[str, Any] = {
            "Bucket": bucket,
            "MaxKeys": max_keys,
        }

        if prefix:
            list_params["Prefix"] = prefix
        if delimiter:
            list_params["Delimiter"] = delimiter
        if params.get("_cursor"):
            list_params["ContinuationToken"] = params["_cursor"]

        # Execute listing
        try:
            response = self.client.list_objects_v2(**list_params)
        except Exception as e:
            logger.error("S3 listing failed", bucket=bucket, prefix=prefix, error=str(e))
            raise StorageListingError(f"Failed to list S3 bucket: {e}") from e

        # Build result arrays
        names: list[str] = []
        paths: list[str] = []
        types: list[str] = []
        sizes: list[int] = []
        modified: list[datetime | None] = []
        etags: list[str | None] = []
        storage_classes: list[str | None] = []
        content_types: list[str | None] = []

        # Add common prefixes (directories)
        for prefix_obj in response.get("CommonPrefixes", []):
            prefix_path = prefix_obj["Prefix"]
            names.append(prefix_path.rstrip("/").split("/")[-1])
            paths.append(prefix_path)
            types.append("directory")
            sizes.append(0)
            modified.append(None)
            etags.append(None)
            storage_classes.append(None)
            content_types.append(None)

        # Add objects (files)
        for obj in response.get("Contents", []):
            key = obj["Key"]
            # Skip the prefix itself if it appears
            if key == prefix:
                continue
            names.append(key.split("/")[-1])
            paths.append(key)
            types.append("file")
            sizes.append(obj.get("Size", 0))
            modified.append(obj.get("LastModified"))
            etags.append(obj.get("ETag", "").strip('"'))
            storage_classes.append(obj.get("StorageClass"))
            content_types.append(None)  # Would need HEAD request

        # Create Arrow table
        table = pa.table(
            {
                "name": names,
                "path": paths,
                "type": types,
                "size": sizes,
                "modified": pa.array(modified, type=pa.timestamp("us", tz="UTC")),
                "etag": etags,
                "storage_class": storage_classes,
                "content_type": content_types,
            },
            schema=LISTING_SCHEMA,
        )

        return table

    def explain(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> ExplainPlan:
        """Return simple explain plan for listing operation."""
        params = params or {}
        bucket = params.get("bucket") or self.target
        prefix = params.get("prefix", query.strip() if query else "")

        return ExplainPlan(
            steps=[
                ExplainStep(
                    step_type=ExplainStepType.READ,
                    description=f"List S3 objects: s3://{bucket}/{prefix}",
                    details={"bucket": bucket, "prefix": prefix},
                ),
            ],
            warnings=[],
        )

    def healthcheck(self) -> bool:
        """Check S3 connectivity."""
        try:
            # Try to list buckets or check bucket exists
            bucket = self.config.get("bucket") or self.target
            if bucket and bucket != "default":
                self.client.head_bucket(Bucket=bucket)
            else:
                self.client.list_buckets()
            return True
        except Exception as e:
            logger.warning("S3 healthcheck failed", error=str(e))
            return False


# MinIO is S3-compatible, register same adapter
@register_adapter("minio")
class MinIOAdapter(S3Adapter):
    """
    MinIO datasource adapter.

    Same as S3Adapter but with MinIO-specific defaults.
    """



@register_adapter("file")
class FilesystemAdapter(DatasourceAdapter):
    """
    Local filesystem datasource adapter for directory listing.

    Target format: base directory path or 'default' for config-based path.

    Security: Only allows listing within configured base paths.
    """

    def __init__(self, target: str, config: dict[str, Any] | None = None):
        super().__init__(target, config)
        self._base_path: Path | None = None

    @property
    def base_path(self) -> Path:
        """Get the base path for filesystem operations."""
        if self._base_path is None:
            # Get from config or settings
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
        """Resolve path safely within base_path (prevent directory traversal)."""
        # Clean and resolve the path
        clean_subpath = subpath.lstrip("/")
        target_path = (self.base_path / clean_subpath).resolve()

        # Ensure it's within base_path
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
    ) -> pa.Table:
        """
        List files in directory.

        Query: subdirectory path to list
        Params:
        - path: Subdirectory path
        - recursive: List recursively (default: False)
        - pattern: Glob pattern filter
        - limit: Maximum entries to return
        - offset: Skip first N entries
        """
        params = params or {}

        # Determine path to list
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

        # Build result arrays
        names: list[str] = []
        paths: list[str] = []
        types: list[str] = []
        sizes: list[int] = []
        modified: list[datetime] = []
        etags: list[str | None] = []
        storage_classes: list[str | None] = []
        content_types: list[str | None] = []

        # Iterate directory
        try:
            if recursive:
                entries = list(target_path.rglob(pattern))
            else:
                entries = list(target_path.glob(pattern))

            # Apply pagination
            entries = sorted(entries, key=lambda p: (p.is_file(), p.name))
            entries = entries[offset : offset + limit]

            for entry in entries:
                stat = entry.stat()
                rel_path = entry.relative_to(self.base_path)

                names.append(entry.name)
                paths.append(str(rel_path))
                types.append("file" if entry.is_file() else "directory")
                sizes.append(stat.st_size if entry.is_file() else 0)
                modified.append(datetime.fromtimestamp(stat.st_mtime))
                etags.append(None)
                storage_classes.append(None)
                # Guess content type from extension
                content_types.append(self._guess_content_type(entry) if entry.is_file() else None)

        except PermissionError as e:
            raise StorageListingError(f"Permission denied: {subpath}") from e
        except Exception as e:
            logger.error("Filesystem listing failed", path=subpath, error=str(e))
            raise StorageListingError(f"Failed to list directory: {e}") from e

        # Create Arrow table
        table = pa.table(
            {
                "name": names,
                "path": paths,
                "type": types,
                "size": sizes,
                "modified": pa.array(modified, type=pa.timestamp("us", tz="UTC")),
                "etag": etags,
                "storage_class": storage_classes,
                "content_type": content_types,
            },
            schema=LISTING_SCHEMA,
        )

        return table

    def _guess_content_type(self, path: Path) -> str | None:
        """Guess content type from file extension."""
        import mimetypes

        mime_type, _ = mimetypes.guess_type(str(path))
        return mime_type

    def explain(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> ExplainPlan:
        """Return simple explain plan for listing operation."""
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
                ),
            ],
            warnings=[],
        )

    def healthcheck(self) -> bool:
        """Check filesystem accessibility."""
        try:
            return self.base_path.exists() and self.base_path.is_dir()
        except Exception as e:
            logger.warning("Filesystem healthcheck failed", error=str(e))
            return False
