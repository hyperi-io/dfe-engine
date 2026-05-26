"""Tests for pagination models and helpers."""

from dfe_engine.api.pagination import PaginatedResponse, apply_search, apply_sort


class TestPaginatedResponse:
    """PaginatedResponse model and from_list()."""

    def test_from_list_basic(self):
        items = list(range(10))
        resp = PaginatedResponse.from_list(items, page=1, per_page=3)
        assert resp.items == [0, 1, 2]
        assert resp.total == 10
        assert resp.page == 1
        assert resp.per_page == 3
        assert resp.total_pages == 4
        assert resp.next_page == 2
        assert resp.prev_page is None

    def test_from_list_last_page(self):
        items = list(range(10))
        resp = PaginatedResponse.from_list(items, page=4, per_page=3)
        assert resp.items == [9]
        assert resp.next_page is None
        assert resp.prev_page == 3

    def test_from_list_middle_page(self):
        items = list(range(10))
        resp = PaginatedResponse.from_list(items, page=2, per_page=3)
        assert resp.items == [3, 4, 5]
        assert resp.next_page == 3
        assert resp.prev_page == 1

    def test_from_list_empty(self):
        resp = PaginatedResponse.from_list([], page=1, per_page=25)
        assert resp.items == []
        assert resp.total == 0
        assert resp.total_pages == 1
        assert resp.next_page is None
        assert resp.prev_page is None

    def test_from_list_single_page(self):
        items = [1, 2, 3]
        resp = PaginatedResponse.from_list(items, page=1, per_page=25)
        assert resp.items == [1, 2, 3]
        assert resp.total_pages == 1
        assert resp.next_page is None

    def test_from_list_beyond_last_page(self):
        items = list(range(5))
        resp = PaginatedResponse.from_list(items, page=100, per_page=3)
        assert resp.items == []
        assert resp.total == 5

    def test_serialization(self):
        resp = PaginatedResponse.from_list(["a", "b"], page=1, per_page=10)
        data = resp.model_dump(mode="json")
        assert data["items"] == ["a", "b"]
        assert data["total"] == 2
        assert data["total_pages"] == 1
        assert data["next_page"] is None
        assert data["prev_page"] is None

    def test_from_list_per_page_minus_one_returns_all(self):
        items = list(range(50))
        resp = PaginatedResponse.from_list(items, page=3, per_page=-1)
        assert resp.items == items
        assert resp.total == 50
        assert resp.page == 1
        assert resp.per_page == -1
        assert resp.total_pages == 1
        assert resp.next_page is None
        assert resp.prev_page is None


class TestApplySearch:
    """apply_search() helper."""

    def test_search_matches(self):
        items = [
            {"name": "Windows Audit", "desc": "Windows security events"},
            {"name": "Linux Syslog", "desc": "Linux system logs"},
            {"name": "AWS CloudTrail", "desc": "Cloud audit logs"},
        ]
        result = apply_search(items, "linux", ["name", "desc"])
        assert len(result) == 1
        assert result[0]["name"] == "Linux Syslog"

    def test_search_case_insensitive(self):
        items = [{"name": "CloudTrail"}]
        result = apply_search(items, "CLOUD", ["name"])
        assert len(result) == 1

    def test_search_none_returns_all(self):
        items = [{"name": "a"}, {"name": "b"}]
        result = apply_search(items, None, ["name"])
        assert len(result) == 2

    def test_search_empty_string_returns_all(self):
        items = [{"name": "a"}, {"name": "b"}]
        result = apply_search(items, "", ["name"])
        assert len(result) == 2

    def test_search_no_match(self):
        items = [{"name": "foo"}]
        result = apply_search(items, "zzz", ["name"])
        assert len(result) == 0

    def test_search_partial_match(self):
        items = [{"name": "windows_audit_2024"}]
        result = apply_search(items, "audit", ["name"])
        assert len(result) == 1


class TestApplySort:
    """apply_sort() helper."""

    def test_sort_asc(self):
        items = [{"name": "c"}, {"name": "a"}, {"name": "b"}]
        result = apply_sort(items, "name", "asc")
        assert [r["name"] for r in result] == ["a", "b", "c"]

    def test_sort_desc(self):
        items = [{"name": "c"}, {"name": "a"}, {"name": "b"}]
        result = apply_sort(items, "name", "desc")
        assert [r["name"] for r in result] == ["c", "b", "a"]

    def test_sort_none_returns_original(self):
        items = [{"name": "c"}, {"name": "a"}]
        result = apply_sort(items, None, "asc")
        assert result == items

    def test_sort_with_missing_key(self):
        items = [{"name": "b"}, {"other": "val"}, {"name": "a"}]
        result = apply_sort(items, "name", "asc")
        # Missing key → empty string, sorts first
        assert result[0].get("name", "") == ""
