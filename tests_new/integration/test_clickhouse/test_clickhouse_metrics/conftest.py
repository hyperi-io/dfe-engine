import pytest

TEST_GET_HUNT_METRICS = [
    {
        "name": "single_hunt",
        "hunt_name": "test_hunt_name_1",
        "data_to_insert": [
            {
                "customer_name": "test_customer_name",
                "rule_name": "test_rule_name",
                "thread_id": "test_threead_id",
                "log_buffer": 60,
                "query_schedule_time": "2026-01-01 01:00:00",
                "execution_time": "2026-01-01 01:00:00",
                "end_time": "2026-01-01 01:00:00",
                "previous_successful_checkpoint": "2026-01-01 01:00:00",
                "query_checkpoint_time": "2026-01-01 01:00:00",
                "execution_time_ms": 1,
                "hunt_name": "test_hunt_name_1",
                "query_id": "test_query_id_1"
            }
        ],
        "expected_result": [(1,)]
    }
]

@pytest.fixture(params = TEST_GET_HUNT_METRICS, ids = lambda x: x["name"])
def test_get_hunt_metrics(request):
    return request.param