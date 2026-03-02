"""
Unit tests for storage datasource adapters.
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pytest

from dfe_engine.query.datasources import get_adapter, list_adapters
from dfe_engine.query.datasources.storage import (
    LISTING_SCHEMA,
    FilesystemAdapter,
    MinIOAdapter,
    S3Adapter,
    StorageListingError,
)
from dfe_engine.query.models import ExplainStepType


class TestStorageAdaptersRegistration:
    """Test that storage adapters are registered correctly."""

    def test_s3_adapter_registered(self):
        """S3 adapter should be registered."""
        adapters = list_adapters()
        assert "s3" in adapters

    def test_minio_adapter_registered(self):
        """MinIO adapter should be registered."""
        adapters = list_adapters()
        assert "minio" in adapters

    def test_file_adapter_registered(self):
        """Filesystem adapter should be registered."""
        adapters = list_adapters()
        assert "file" in adapters

    def test_get_s3_adapter(self):
        """Get S3 adapter by datasource URI."""
        adapter = get_adapter("s3:my-bucket")
        assert isinstance(adapter, S3Adapter)
        assert adapter.target == "my-bucket"

    def test_get_minio_adapter(self):
        """Get MinIO adapter by datasource URI."""
        adapter = get_adapter("minio:my-bucket")
        assert isinstance(adapter, MinIOAdapter)
        assert adapter.target == "my-bucket"

    def test_get_file_adapter(self):
        """Get filesystem adapter by datasource URI."""
        adapter = get_adapter("file:default")
        assert isinstance(adapter, FilesystemAdapter)
        assert adapter.target == "default"


class TestListingSchema:
    """Test the standard listing schema."""

    def test_schema_fields(self):
        """Schema should have expected fields."""
        field_names = {f.name for f in LISTING_SCHEMA}
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
        assert field_names == expected

    def test_schema_types(self):
        """Schema fields should have correct types."""
        fields = {f.name: f.type for f in LISTING_SCHEMA}
        assert fields["name"] == pa.string()
        assert fields["path"] == pa.string()
        assert fields["type"] == pa.string()
        assert fields["size"] == pa.int64()
        assert pa.types.is_timestamp(fields["modified"])


class TestS3Adapter:
    """Test S3 adapter functionality."""

    @pytest.fixture
    def mock_s3_response(self):
        """Mock S3 list_objects_v2 response."""
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
            "CommonPrefixes": [
                {"Prefix": "path/to/subdir/"},
            ],
            "IsTruncated": False,
        }

    def test_execute_returns_arrow_table(self, mock_s3_response):
        """Execute should return Arrow table."""
        adapter = S3Adapter("test-bucket")

        with patch.object(adapter, "_client") as mock_client:
            mock_client.list_objects_v2.return_value = mock_s3_response
            adapter._client = mock_client

            result = adapter.execute("path/to/", params={"bucket": "test-bucket"})

        assert isinstance(result, pa.Table)
        assert result.schema == LISTING_SCHEMA

    def test_execute_includes_directories(self, mock_s3_response):
        """Execute should include directories from CommonPrefixes."""
        adapter = S3Adapter("test-bucket")

        with patch.object(adapter, "_client") as mock_client:
            mock_client.list_objects_v2.return_value = mock_s3_response
            adapter._client = mock_client

            result = adapter.execute("path/to/", params={"bucket": "test-bucket"})

        # Check that directory is included
        types = result.column("type").to_pylist()
        assert "directory" in types

        # Check directory name
        names = result.column("name").to_pylist()
        assert "subdir" in names

    def test_execute_includes_files(self, mock_s3_response):
        """Execute should include files from Contents."""
        adapter = S3Adapter("test-bucket")

        with patch.object(adapter, "_client") as mock_client:
            mock_client.list_objects_v2.return_value = mock_s3_response
            adapter._client = mock_client

            result = adapter.execute("path/to/", params={"bucket": "test-bucket"})

        # Check that files are included
        types = result.column("type").to_pylist()
        assert types.count("file") == 2

        names = result.column("name").to_pylist()
        assert "file1.txt" in names
        assert "file2.json" in names

    def test_execute_uses_cursor_for_pagination(self, mock_s3_response):
        """Execute should use cursor for continuation."""
        adapter = S3Adapter("test-bucket")

        with patch.object(adapter, "_client") as mock_client:
            mock_client.list_objects_v2.return_value = mock_s3_response
            adapter._client = mock_client

            adapter.execute(
                "path/",
                params={"bucket": "test-bucket", "_cursor": "next-token-123"},
            )

        # Verify ContinuationToken was passed
        call_args = mock_client.list_objects_v2.call_args
        assert call_args[1]["ContinuationToken"] == "next-token-123"

    def test_explain_returns_plan(self):
        """Explain should return ExplainPlan."""
        adapter = S3Adapter("test-bucket")

        plan = adapter.explain("path/to/", params={"bucket": "test-bucket"})

        assert len(plan.steps) == 1
        assert plan.steps[0].step_type == ExplainStepType.READ
        assert "s3://" in plan.steps[0].description

    def test_execute_raises_on_error(self):
        """Execute should raise StorageListingError on S3 errors."""
        adapter = S3Adapter("test-bucket")

        with patch.object(adapter, "_client") as mock_client:
            mock_client.list_objects_v2.side_effect = Exception("Access denied")
            adapter._client = mock_client

            with pytest.raises(StorageListingError, match="Failed to list S3"):
                adapter.execute("path/", params={"bucket": "test-bucket"})


class TestFilesystemAdapter:
    """Test filesystem adapter functionality."""

    @pytest.fixture
    def temp_directory(self):
        """Create a temporary directory with test files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)

            # Create directory structure
            (base / "subdir1").mkdir()
            (base / "subdir2").mkdir()
            (base / "file1.txt").write_text("content1")
            (base / "file2.json").write_text('{"key": "value"}')
            (base / "subdir1" / "nested.txt").write_text("nested content")

            yield tmpdir

    def test_execute_returns_arrow_table(self, temp_directory):
        """Execute should return Arrow table."""
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})

        result = adapter.execute("")

        assert isinstance(result, pa.Table)
        assert result.schema == LISTING_SCHEMA

    def test_execute_lists_files_and_directories(self, temp_directory):
        """Execute should list both files and directories."""
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})

        result = adapter.execute("")

        types = result.column("type").to_pylist()
        assert "file" in types
        assert "directory" in types

        names = result.column("name").to_pylist()
        assert "file1.txt" in names
        assert "subdir1" in names

    def test_execute_with_subdirectory(self, temp_directory):
        """Execute should list contents of subdirectory."""
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})

        result = adapter.execute("", params={"path": "subdir1"})

        names = result.column("name").to_pylist()
        assert "nested.txt" in names

    def test_execute_recursive(self, temp_directory):
        """Execute with recursive=True should list all files."""
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})

        result = adapter.execute("", params={"recursive": True})

        paths = result.column("path").to_pylist()
        # Should include nested file
        nested_paths = [p for p in paths if "subdir1" in p]
        assert len(nested_paths) > 0

    def test_execute_with_pattern(self, temp_directory):
        """Execute with pattern should filter by glob."""
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})

        result = adapter.execute("", params={"pattern": "*.txt"})

        names = result.column("name").to_pylist()
        assert "file1.txt" in names
        assert "file2.json" not in names

    def test_execute_with_limit(self, temp_directory):
        """Execute with limit should restrict results."""
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})

        result = adapter.execute("", params={"limit": 2})

        assert result.num_rows == 2

    def test_execute_with_offset(self, temp_directory):
        """Execute with offset should skip entries."""
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})

        result_all = adapter.execute("")
        result_offset = adapter.execute("", params={"offset": 1})

        # Offset result should have one fewer row
        assert result_offset.num_rows == result_all.num_rows - 1

    def test_execute_prevents_directory_traversal(self, temp_directory):
        """Execute should prevent path traversal attacks."""
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})

        with pytest.raises(StorageListingError, match="Path traversal"):
            adapter.execute("", params={"path": "../../../etc"})

    def test_execute_raises_on_nonexistent_path(self, temp_directory):
        """Execute should raise for non-existent paths."""
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})

        with pytest.raises(StorageListingError, match="does not exist"):
            adapter.execute("", params={"path": "nonexistent"})

    def test_execute_raises_on_file_path(self, temp_directory):
        """Execute should raise when path is a file, not directory."""
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})

        with pytest.raises(StorageListingError, match="not a directory"):
            adapter.execute("", params={"path": "file1.txt"})

    def test_explain_returns_plan(self, temp_directory):
        """Explain should return ExplainPlan."""
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})

        plan = adapter.explain("", params={"path": "subdir1"})

        assert len(plan.steps) == 1
        assert plan.steps[0].step_type == ExplainStepType.READ
        assert "filesystem" in plan.steps[0].description.lower()

    def test_healthcheck(self, temp_directory):
        """Healthcheck should return True for valid base path."""
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})

        assert adapter.healthcheck() is True

    def test_healthcheck_false_for_invalid_path(self):
        """Healthcheck should return False for invalid path."""
        adapter = FilesystemAdapter(
            "default", config={"base_path": "/nonexistent/path/12345"}
        )
        adapter._base_path = Path("/nonexistent/path/12345")

        assert adapter.healthcheck() is False

    def test_content_type_guessing(self, temp_directory):
        """Execute should guess content types for files."""
        adapter = FilesystemAdapter("default", config={"base_path": temp_directory})

        result = adapter.execute("")

        content_types = result.column("content_type").to_pylist()
        # At least one should have a content type
        non_null_types = [ct for ct in content_types if ct is not None]
        assert len(non_null_types) > 0


class TestMinIOAdapter:
    """Test MinIO adapter (inherits from S3)."""

    def test_minio_is_s3_compatible(self):
        """MinIO adapter should inherit from S3Adapter."""
        assert issubclass(MinIOAdapter, S3Adapter)

    def test_minio_can_be_instantiated(self):
        """MinIO adapter should be instantiable."""
        adapter = MinIOAdapter("my-bucket")
        assert adapter.target == "my-bucket"
