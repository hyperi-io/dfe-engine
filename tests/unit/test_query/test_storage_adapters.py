"""Unit tests for storage datasource adapters."""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from dfe_engine.query.datasources import get_adapter, list_adapters
from dfe_engine.query.datasources.storage import (
    LISTING_COLUMNS,
    FilesystemAdapter,
    MinIOAdapter,
    S3Adapter,
    StorageListingError,
)
from dfe_engine.query.models import ExplainStepType


class TestStorageAdaptersRegistration:
    def test_s3_adapter_registered(self):
        assert "s3" in list_adapters()

    def test_minio_adapter_registered(self):
        assert "minio" in list_adapters()

    def test_file_adapter_registered(self):
        assert "file" in list_adapters()

    def test_get_s3_adapter(self):
        adapter = get_adapter("s3:my-bucket")
        assert isinstance(adapter, S3Adapter)
        assert adapter.target == "my-bucket"

    def test_get_minio_adapter(self):
        adapter = get_adapter("minio:my-bucket")
        assert isinstance(adapter, MinIOAdapter)

    def test_get_file_adapter(self):
        adapter = get_adapter("file:default")
        assert isinstance(adapter, FilesystemAdapter)


class TestListingColumns:
    def test_column_names(self):
        expected = {
            "name",
            "path",
            "type",
            "size",
            "modified",
            "etag",
            "storage_class",
            "content_type",
        }
        assert set(LISTING_COLUMNS) == expected


class TestS3Adapter:
    @pytest.fixture
    def mock_s3_response(self):
        return {
            "Contents": [
                {
                    "Key": "path/to/file1.txt",
                    "Size": 1024,
                    "LastModified": datetime(2024, 1, 15, 12, 0, 0),
                    "ETag": '"abc123"',
                    "StorageClass": "STANDARD",
                },
                {
                    "Key": "path/to/file2.json",
                    "Size": 2048,
                    "LastModified": datetime(2024, 1, 16, 12, 0, 0),
                    "ETag": '"def456"',
                    "StorageClass": "STANDARD",
                },
            ],
            "CommonPrefixes": [{"Prefix": "path/to/subdir/"}],
            "IsTruncated": False,
        }

    def test_execute_returns_rows_and_columns(self, mock_s3_response):
        adapter = S3Adapter("test-bucket")
        with patch.object(adapter, "_client") as mock_client:
            mock_client.list_objects_v2.return_value = mock_s3_response
            adapter._client = mock_client
            rows, columns = adapter.execute("path/to/", params={"bucket": "test-bucket"})

        assert isinstance(rows, list)
        assert columns == LISTING_COLUMNS

    def test_execute_includes_directories(self, mock_s3_response):
        adapter = S3Adapter("test-bucket")
        with patch.object(adapter, "_client") as mock_client:
            mock_client.list_objects_v2.return_value = mock_s3_response
            adapter._client = mock_client
            rows, _ = adapter.execute("path/to/", params={"bucket": "test-bucket"})

        types = [r["type"] for r in rows]
        assert "directory" in types
        names = [r["name"] for r in rows]
        assert "subdir" in names

    def test_execute_includes_files(self, mock_s3_response):
        adapter = S3Adapter("test-bucket")
        with patch.object(adapter, "_client") as mock_client:
            mock_client.list_objects_v2.return_value = mock_s3_response
            adapter._client = mock_client
            rows, _ = adapter.execute("path/to/", params={"bucket": "test-bucket"})

        types = [r["type"] for r in rows]
        assert types.count("file") == 2
        names = [r["name"] for r in rows]
        assert "file1.txt" in names
        assert "file2.json" in names

    def test_execute_uses_cursor_for_pagination(self, mock_s3_response):
        adapter = S3Adapter("test-bucket")
        with patch.object(adapter, "_client") as mock_client:
            mock_client.list_objects_v2.return_value = mock_s3_response
            adapter._client = mock_client
            adapter.execute("path/", params={"bucket": "test-bucket", "_cursor": "next-token-123"})

        call_args = mock_client.list_objects_v2.call_args
        assert call_args[1]["ContinuationToken"] == "next-token-123"

    def test_explain_returns_plan(self):
        adapter = S3Adapter("test-bucket")
        plan = adapter.explain("path/to/", params={"bucket": "test-bucket"})
        assert len(plan.steps) == 1
        assert plan.steps[0].step_type == ExplainStepType.READ
        assert "s3://" in plan.steps[0].description

    def test_execute_raises_on_error(self):
        adapter = S3Adapter("test-bucket")
        with patch.object(adapter, "_client") as mock_client:
            mock_client.list_objects_v2.side_effect = Exception("Access denied")
            adapter._client = mock_client
            with pytest.raises(StorageListingError, match="Failed to list S3"):
                adapter.execute("path/", params={"bucket": "test-bucket"})


class TestFilesystemAdapter:
    @pytest.fixture
    def temp_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "subdir1").mkdir()
            (base / "subdir2").mkdir()
            (base / "file1.txt").write_text("content1")
            (base / "file2.json").write_text('{"key": "value"}')
            (base / "subdir1" / "nested.txt").write_text("nested content")
            yield tmpdir

    def test_execute_returns_rows_and_columns(self, temp_directory):
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})
        rows, columns = adapter.execute("")
        assert isinstance(rows, list)
        assert columns == LISTING_COLUMNS

    def test_execute_lists_files_and_directories(self, temp_directory):
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})
        rows, _ = adapter.execute("")
        types = [r["type"] for r in rows]
        assert "file" in types
        assert "directory" in types
        names = [r["name"] for r in rows]
        assert "file1.txt" in names
        assert "subdir1" in names

    def test_execute_with_subdirectory(self, temp_directory):
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})
        rows, _ = adapter.execute("", params={"path": "subdir1"})
        names = [r["name"] for r in rows]
        assert "nested.txt" in names

    def test_execute_recursive(self, temp_directory):
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})
        rows, _ = adapter.execute("", params={"recursive": True})
        paths = [r["path"] for r in rows]
        nested = [p for p in paths if "subdir1" in p]
        assert len(nested) > 0

    def test_execute_with_pattern(self, temp_directory):
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})
        rows, _ = adapter.execute("", params={"pattern": "*.txt"})
        names = [r["name"] for r in rows]
        assert "file1.txt" in names
        assert "file2.json" not in names

    def test_execute_with_limit(self, temp_directory):
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})
        rows, _ = adapter.execute("", params={"limit": 2})
        assert len(rows) == 2

    def test_execute_with_offset(self, temp_directory):
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})
        rows_all, _ = adapter.execute("")
        rows_offset, _ = adapter.execute("", params={"offset": 1})
        assert len(rows_offset) == len(rows_all) - 1

    def test_execute_prevents_directory_traversal(self, temp_directory):
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})
        with pytest.raises(StorageListingError, match="Path traversal"):
            adapter.execute("", params={"path": "../../../etc"})

    def test_execute_raises_on_nonexistent_path(self, temp_directory):
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})
        with pytest.raises(StorageListingError, match="does not exist"):
            adapter.execute("", params={"path": "nonexistent"})

    def test_execute_raises_on_file_path(self, temp_directory):
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})
        with pytest.raises(StorageListingError, match="not a directory"):
            adapter.execute("", params={"path": "file1.txt"})

    def test_explain_returns_plan(self, temp_directory):
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})
        plan = adapter.explain("", params={"path": "subdir1"})
        assert len(plan.steps) == 1
        assert plan.steps[0].step_type == ExplainStepType.READ

    def test_healthcheck(self, temp_directory):
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})
        assert adapter.healthcheck() is True

    def test_healthcheck_false_for_invalid_path(self):
        adapter = FilesystemAdapter("default", config={"base_path": "/nonexistent/path/12345"})
        adapter._base_path = Path("/nonexistent/path/12345")
        assert adapter.healthcheck() is False

    def test_content_type_guessing(self, temp_directory):
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})
        rows, _ = adapter.execute("")
        content_types = [r["content_type"] for r in rows]
        non_null = [ct for ct in content_types if ct is not None]
        assert len(non_null) > 0


class TestMinIOAdapter:
    def test_minio_is_s3_compatible(self):
        assert issubclass(MinIOAdapter, S3Adapter)

    def test_minio_can_be_instantiated(self):
        adapter = MinIOAdapter("my-bucket")
        assert adapter.target == "my-bucket"
