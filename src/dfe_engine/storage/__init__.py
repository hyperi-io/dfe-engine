"""
Storage abstraction module for DFE Engine.

Provides a unified interface for accessing files from different storage backends:
- Local filesystem (for on-prem/Rancher deployments with PVC mounts)
- HTTP/HTTPS (for Artifactory or other HTTP-based artifact servers)
- S3 (for AWS deployments)

Usage:
    from dfe_engine.storage import get_storage_backend, StorageBackend

    # Auto-sensing based on path
    backend = get_storage_backend("/mnt/artifacts")  # Returns LocalStorageBackend
    backend = get_storage_backend("https://artifactory.example.com/...")  # Returns HTTPStorageBackend
    backend = get_storage_backend("s3://my-bucket/path")  # Returns S3StorageBackend

    # Download a file
    backend.download("templates.zip", "/local/path/templates.zip")

    # List files
    files = backend.list_files("templates/")
"""

from .backends import (
    HTTPStorageBackend,
    LocalStorageBackend,
    S3StorageBackend,
    StorageBackend,
    StorageError,
    get_storage_backend,
)

__all__ = [
    "HTTPStorageBackend",
    "LocalStorageBackend",
    "S3StorageBackend",
    "StorageBackend",
    "StorageError",
    "get_storage_backend",
]
