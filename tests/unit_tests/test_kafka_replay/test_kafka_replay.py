import pytest
from test_data import TEST_PARSE_TIMESTAMP, TEST_CREATE_KEY_PREFIXES, TEST_MANIPULATE_LOG
from dfecli.dfe_kafka_replay.replay_s3_to_kafka import ReplayS3ToKafka


@pytest.mark.parametrize("test_case", TEST_PARSE_TIMESTAMP, ids = lambda x: x["name"])
def test_parse_timestamp(test_case):
    if (test_case["raises"]):
        with pytest.raises(test_case["raises"]["exception"]) as exc_info:
            ReplayS3ToKafka.parse_timestamp(test_case["inputs"]["timestamp_to_parse"])
        assert test_case["raises"]["message"] in str(exc_info.value)
    else:
        result = ReplayS3ToKafka.parse_timestamp(test_case["inputs"]["timestamp_to_parse"])
        assert (result == test_case["expected_output"])


@pytest.mark.parametrize("test_case", TEST_CREATE_KEY_PREFIXES, ids = lambda x: x["name"])
def test_create_key_prefixes(test_case):
    if (test_case["raises"]):
        with pytest.raises(test_case["raises"]["exception"]) as exc_info:
            result = ReplayS3ToKafka.create_key_prefixes(
                start_time = ReplayS3ToKafka.parse_timestamp(test_case["inputs"]["start_time"]),
                end_time = ReplayS3ToKafka.parse_timestamp(test_case["inputs"]["end_time"])
            )
        assert test_case["raises"]["message"] in str(exc_info.value)
    else:
        result = ReplayS3ToKafka.create_key_prefixes(
            start_time = ReplayS3ToKafka.parse_timestamp(test_case["inputs"]["start_time"]),
            end_time = ReplayS3ToKafka.parse_timestamp(test_case["inputs"]["end_time"])
        )
        assert (result == test_case["expected_output"])

@pytest.mark.parametrize("test_case", TEST_MANIPULATE_LOG, ids = lambda x: x["name"])
def test_manipulate_log(test_case):
    if (test_case["raises"]):
        with pytest.raises(test_case["raises"]["exception"]) as exc_info:
            result = ReplayS3ToKafka.manipulate_log(
                set_timestamps = test_case["inputs"]["set_timestamps"],
                log = test_case["inputs"]["log"],
                timestamp_to_set = test_case["inputs"].get("timestamp_to_set", None)

            )
        assert test_case["raises"]["message"] in str(exc_info.value)
    else:
        result = ReplayS3ToKafka.manipulate_log(
            set_timestamps = test_case["inputs"]["set_timestamps"],
            log = test_case["inputs"]["log"],
            timestamp_to_set = test_case["inputs"].get("timestamp_to_set", None)
        )
        assert (result == test_case["expected_output"])