"""The e2e row counter matches its marker on the payload, wherever it was stored."""

from tests.e2e.conftest import PAYLOAD_TEXT, count_rows


class _Result:
    result_rows = [(1,)]


class _RecordingClient:
    """Answers every query with one row and keeps the SQL and parameters it was sent."""

    def __init__(self) -> None:
        self.sql = ""
        self.parameters: dict[str, str] = {}

    def query(self, sql: str, parameters: dict[str, str] | None = None) -> _Result:
        self.sql = sql
        self.parameters = parameters or {}
        return _Result()


def test_the_marker_is_matched_on_json_or_raw_text() -> None:
    # A JSON payload lands with _raw NULL, so a bare _raw match can never count it.
    client = _RecordingClient()
    count_rows(client, "dfe.main", marker="mk-1", contains="needle")
    assert f"{PAYLOAD_TEXT} LIKE %(m)s" in client.sql
    assert f"{PAYLOAD_TEXT} LIKE %(c)s" in client.sql
    assert "WHERE _raw LIKE" not in client.sql
    assert client.parameters == {"m": "%mk-1%", "c": "%needle%"}


def test_the_payload_text_reads_raw_first_then_json() -> None:
    assert PAYLOAD_TEXT == "coalesce(_raw, toString(_json))"


def test_a_count_with_no_marker_adds_no_payload_match() -> None:
    client = _RecordingClient()
    count_rows(client, "dfe.main", where="_source = 'x'")
    assert PAYLOAD_TEXT not in client.sql
