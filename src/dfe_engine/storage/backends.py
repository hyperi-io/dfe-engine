"""
Storage backend implementations for DFE Engine.

Supports local filesystem, HTTP, and S3 storage backends with auto-sensing.
"""

import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

import httpx
from hyperi_pylib.http import HttpClient
from hyperi_pylib.logger import logger


class StorageError(Exception):
    """Exception raised for storage operation errors."""

    def __init__(self, message: str, cause: Optional[Exception] = None):
        super().__init__(message)
        self.message = message
        self.cause = cause


class StorageBackend(ABC):
    """Abstract base class for storage backends."""

    @abstractmethod
    def download(self, remote_path: str, local_path: str) -> None:
        """
        Download a file from the storage backend to a local path.

        Args:
            remote_path: Path or identifier of the file in the storage backend.
            local_path: Local filesystem path to save the file.

        Raises:
            StorageError: If the download fails.
        """
        pass

    @abstractmethod
    def list_files(self, prefix: str = "") -> List[str]:
        """
        List files in the storage backend.

        Args:
            prefix: Optional prefix/path to filter files.

        Returns:
            List of file paths/names.

        Raises:
            StorageError: If listing fails.
        """
        pass

    @abstractmethod
    def exists(self, path: str) -> bool:
        """
        Check if a file exists in the storage backend.

        Args:
            path: Path to check.

        Returns:
            True if the file exists, False otherwise.
        """
        pass


class LocalStorageBackend(StorageBackend):
    """
    Local filesystem storage backend.

    Use for on-prem deployments with mounted volumes (PVC, NFS, etc.).
    """

    def __init__(self, base_path: str):
        """
        Initialize local storage backend.

        Args:
            base_path: Base directory path for storage operations.
        """
        self.base_path = Path(base_path).resolve()
        logger.info(f"Initialized LocalStorageBackend with base_path: {self.base_path}")

    def _resolve_path(self, path: str) -> Path:
        """Resolve a path relative to the base path."""
        if Path(path).is_absolute():
            return Path(path)
        return self.base_path / path

    def download(self, remote_path: str, local_path: str) -> None:
        """Copy a file from the storage location to the local path."""
        source = self._resolve_path(remote_path)
        dest = Path(local_path)

        if not source.exists():
            raise StorageError(f"Source file not found: {source}")

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(source, dest)
            else:
                shutil.copy2(source, dest)
            logger.info(f"Copied {source} to {dest}")
        except Exception as e:
            raise StorageError(f"Failed to copy {source} to {dest}: {e}", cause=e) from e

    def list_files(self, prefix: str = "") -> List[str]:
        """List files in the storage directory."""
        search_path = self._resolve_path(prefix)

        if not search_path.exists():
            return []

        try:
            if search_path.is_file():
                return [str(search_path)]

            files = []
            for item in search_path.rglob("*"):
                if item.is_file():
                    files.append(str(item.relative_to(self.base_path)))
            return files
        except Exception as e:
            raise StorageError(f"Failed to list files in {search_path}: {e}", cause=e) from e

    def exists(self, path: str) -> bool:
        """Check if a file exists."""
        return self._resolve_path(path).exists()


class HTTPStorageBackend(StorageBackend):
    """
    HTTP/HTTPS storage backend.

    Use for Artifactory or other HTTP-based artifact servers.
    """

    def __init__(
        self,
        base_url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = 120.0,
    ):
        """
        Initialize HTTP storage backend.

        Args:
            base_url: Base URL for the storage server.
            username: Optional username for authentication.
            password: Optional password for authentication.
            timeout: Request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.auth = (username, password) if username and password else None
        self._client = HttpClient(
            base_url=self.base_url,
            timeout=timeout,
            retries=3,
            auth=self.auth,
            follow_redirects=True,
        )
        logger.info(f"Initialized HTTPStorageBackend with base_url: {self.base_url}")

    def _build_url(self, path: str) -> str:
        """Build full URL from path."""
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def download(self, remote_path: str, local_path: str) -> None:
        """Download a file from the HTTP server."""
        url = self._build_url(remote_path)
        dest = Path(local_path)

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)

            with self._client._client.stream("GET", url) as response:
                response.raise_for_status()

                with open(dest, "wb") as f:
                    for chunk in response.iter_bytes():
                        f.write(chunk)

            logger.info(f"Downloaded {url} to {dest}")

        except httpx.HTTPStatusError as e:
            raise StorageError(
                f"HTTP error downloading {url}: {e.response.status_code} {e.response.reason_phrase}",
                cause=e,
            )
        except httpx.RequestError as e:
            raise StorageError(f"Request error downloading {url}: {e}", cause=e)
        except Exception as e:
            raise StorageError(f"Failed to download {url}: {e}", cause=e) from e

    def list_files(self, prefix: str = "") -> List[str]:
        """
        List files is not typically supported for HTTP backends.

        Returns an empty list. Use specific API endpoints if available.
        """
        logger.warning("HTTPStorageBackend.list_files() not supported - returning empty list")
        return []

    def exists(self, path: str) -> bool:
        """Check if a file exists by making a HEAD request."""
        url = self._build_url(path)
        try:
            response = self._client.head(url)
            return response.status_code == 200
        except Exception:
            return False


class S3StorageBackend(StorageBackend):
    """
    AWS S3 storage backend.

    Use for AWS deployments with S3 buckets.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        region: Optional[str] = None,
    ):
        """
        Initialize S3 storage backend.

        Args:
            bucket: S3 bucket name.
            prefix: Optional prefix for all paths.
            region: Optional AWS region.
        """
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.region = region
        self._client = None
        logger.info(f"Initialized S3StorageBackend with bucket: {bucket}, prefix: {prefix}")

    @property
    def client(self):
        """Lazy-load the S3 client."""
        if self._client is None:
            import boto3

            self._client = boto3.client("s3", region_name=self.region)
        return self._client

    def _build_key(self, path: str) -> str:
        """Build S3 key from path."""
        path = path.lstrip("/")
        if self.prefix:
            return f"{self.prefix}/{path}"
        return path

    def download(self, remote_path: str, local_path: str) -> None:
        """Download a file from S3."""
        key = self._build_key(remote_path)
        dest = Path(local_path)

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            self.client.download_file(self.bucket, key, str(dest))
            logger.info(f"Downloaded s3://{self.bucket}/{key} to {dest}")
        except Exception as e:
            raise StorageError(f"Failed to download s3://{self.bucket}/{key}: {e}", cause=e)

    def list_files(self, prefix: str = "") -> List[str]:
        """List files in S3 bucket with prefix."""
        search_prefix = self._build_key(prefix)

        try:
            files = []
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=search_prefix):
                for obj in page.get("Contents", []):
                    files.append(obj["Key"])
            return files
        except Exception as e:
            raise StorageError(
                f"Failed to list files in s3://{self.bucket}/{search_prefix}: {e}",
                cause=e,
            )

    def exists(self, path: str) -> bool:
        """Check if a file exists in S3."""
        key = self._build_key(path)
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False


def get_storage_backend(
    path: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> StorageBackend:
    """
    Auto-detect and return the appropriate storage backend based on the path.

    Args:
        path: Storage path - can be local path, HTTP URL, or S3 URI.
        username: Optional username for HTTP auth.
        password: Optional password for HTTP auth.

    Returns:
        Appropriate StorageBackend instance.

    Examples:
        get_storage_backend("/mnt/artifacts")  # LocalStorageBackend
        get_storage_backend("./local/path")  # LocalStorageBackend
        get_storage_backend("https://artifactory.example.com")  # HTTPStorageBackend
        get_storage_backend("s3://my-bucket/path")  # S3StorageBackend
    """
    # Parse the path to determine backend type
    if path.startswith("s3://"):
        # S3 URI: s3://bucket/prefix
        parsed = path[5:]  # Remove "s3://"
        parts = parsed.split("/", 1)
        bucket = parts[0]
        prefix = parts[1] if len(parts) > 1 else ""
        logger.info(f"Auto-detected S3 storage backend for: {path}")
        return S3StorageBackend(bucket=bucket, prefix=prefix)

    elif path.startswith("http://") or path.startswith("https://"):
        # HTTP/HTTPS URL
        logger.info(f"Auto-detected HTTP storage backend for: {path}")
        return HTTPStorageBackend(base_url=path, username=username, password=password)

    else:
        # Local filesystem path (absolute or relative)
        logger.info(f"Auto-detected local storage backend for: {path}")
        return LocalStorageBackend(base_path=path)
