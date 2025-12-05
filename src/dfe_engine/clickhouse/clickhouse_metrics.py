from typing import Dict
from hs_lib.logger import logger
from .clickhouse_manager import ClickHouseManager

logger = DFELog().log

class ClickHouseMetrics:
    """Class for retrieving metrics from ClickHouse using the connection pool."""

    def __init__(self, target_config_data: dict = None):
        """Initialize with target configuration data."""
        self.ch_manager = ClickHouseManager.get_instance(logger, target_config_data)

    def get_hunt_metrics(self, hunt_name: str) -> Dict:
        """
        Get metrics for a specific hunt from ClickHouse.

        Args:
            hunt_name: Name of the hunt to get metrics for

        Returns:
            Dict containing hunt metrics
        """
        try:
            client = self.ch_manager.get_clickhouse_client()
            
            # Query for total executions
            total_executions = client.execute(
                f"""
                SELECT COUNT(*)
                FROM xdr_audit
                WHERE hunt_name = '{hunt_name}'
                """
            )

            # Query for last execution time
            last_execution = client.execute(
                f"""
                SELECT MAX(timestamp)
                FROM xdr_audit
                WHERE hunt_name = '{hunt_name}'
                """
            )

            # Query for average execution time
            avg_execution_time = client.execute(
                f"""
                SELECT AVG(execution_time)
                FROM xdr_audit
                WHERE hunt_name = '{hunt_name}'
                """
            )

            # Query for total matches
            total_matches = client.execute(
                f"""
                SELECT COUNT(*)
                FROM xdr_audit
                WHERE hunt_name = '{hunt_name}'
                AND result = 'match'
                """
            )

            return {
                "total_executions": total_executions[0][0] if total_executions else 0,
                "last_execution_time": last_execution[0][0].strftime('%Y-%m-%d %H:%M:%S') if last_execution and last_execution[0][0] else None,
                "average_execution_time": float(avg_execution_time[0][0]) if avg_execution_time and avg_execution_time[0][0] else 0.0,
                "total_matches": total_matches[0][0] if total_matches else 0
            }

        except Exception as e:
            logger.error(f"Error getting hunt metrics from ClickHouse: {str(e)}")
            return {
                "total_executions": 0,
                "last_execution_time": None,
                "average_execution_time": 0.0,
                "total_matches": 0
            }