"""
Integration tests to verify Docker fixtures work correctly.

These tests validate that:
1. Docker containers start automatically when needed
2. Test databases are created with unique names
3. Database clients connect properly
4. Test databases are cleaned up after tests
"""

import pytest


class TestDockerFixtures:
    """Test the Docker fixture infrastructure."""

    @pytest.mark.integration
    def test_docker_services_available(self, docker_services):
        """Verify Docker services fixture returns status."""
        assert isinstance(docker_services, dict)
        assert "clickhouse" in docker_services
        assert "postgres" in docker_services

    @pytest.mark.integration
    def test_test_run_id_unique(self, test_run_id):
        """Verify test run ID is generated."""
        assert test_run_id is not None
        assert len(test_run_id) == 8
        assert test_run_id.isalnum()

    @pytest.mark.integration
    def test_clickhouse_test_database_created(self, clickhouse_test_database):
        """Verify ClickHouse test database is created."""
        assert clickhouse_test_database is not None
        assert clickhouse_test_database.startswith("dfe_test_")

    @pytest.mark.integration
    def test_clickhouse_client_works(self, clickhouse_client, clickhouse_test_database):
        """Verify ClickHouse client can execute queries."""
        # Create a test table
        clickhouse_client.command(
            f"CREATE TABLE IF NOT EXISTS {clickhouse_test_database}.test_table (id UInt32) ENGINE = Memory"
        )

        # Insert data
        clickhouse_client.command(
            f"INSERT INTO {clickhouse_test_database}.test_table VALUES (1), (2), (3)"
        )

        # Query data
        result = clickhouse_client.query(
            f"SELECT count() FROM {clickhouse_test_database}.test_table"
        )
        assert result.result_rows[0][0] == 3
