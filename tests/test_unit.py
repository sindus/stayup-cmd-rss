"""Unit tests — no external dependencies (DB, network)."""

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fetch_rss import (
    cleanup_old_entries,
    fetch_feed_entries,
    get_latest_entry,
    init_db,
    save_entry,
    save_error,
    upsert_repository,
)

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def make_conn_mock():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cursor


class TestInitDb:
    def test_executes_ddl_and_commits(self):
        conn, cursor = make_conn_mock()
        init_db(conn)
        assert cursor.execute.call_count == 1
        conn.commit.assert_called_once()

    def test_ddl_registers_the_provider(self):
        conn, cursor = make_conn_mock()
        init_db(conn)
        sql = cursor.execute.call_args[0][0]
        assert "CREATE TABLE IF NOT EXISTS provider_registry" in sql
        assert "INSERT INTO provider_registry" in sql
        assert "'rss', 'RSS'" in sql


class TestUpsertRepository:
    def test_returns_id(self):
        conn, cursor = make_conn_mock()
        cursor.fetchone.return_value = (7,)
        result = upsert_repository(conn, "https://example.com/feed.xml")
        assert result == 7
        sql = cursor.execute.call_args[0][0]
        assert "INSERT INTO repository" in sql
        assert "ON CONFLICT" in sql

    def test_passes_url_as_parameter(self):
        conn, cursor = make_conn_mock()
        cursor.fetchone.return_value = (1,)
        upsert_repository(conn, "https://example.com/feed.xml")
        params = cursor.execute.call_args[0][1]
        assert params == ("https://example.com/feed.xml",)


class TestGetLatestEntry:
    def test_returns_none_when_no_row(self):
        conn, cursor = make_conn_mock()
        cursor.fetchone.return_value = None
        version = get_latest_entry(conn, 1)
        assert version is None

    def test_returns_version_when_row_exists(self):
        conn, cursor = make_conn_mock()
        cursor.fetchone.return_value = ("https://example.com/entry/1",)
        version = get_latest_entry(conn, 1)
        assert version == "https://example.com/entry/1"

    def test_queries_by_repository_id(self):
        conn, cursor = make_conn_mock()
        cursor.fetchone.return_value = None
        get_latest_entry(conn, 42)
        params = cursor.execute.call_args[0][1]
        assert params == (42,)


class TestSaveEntry:
    def test_inserts_with_version_and_commits(self):
        conn, cursor = make_conn_mock()
        executed_at = datetime.now(tz=timezone.utc)
        content = '{"version": "guid-123", "title": "test"}'
        save_entry(conn, 1, content, None, executed_at)
        cursor.execute.assert_called_once()
        conn.commit.assert_called_once()
        params = cursor.execute.call_args[0][1]
        assert params[0] == 1
        assert params[1] == content
        assert params[3] == executed_at

    def test_success_flag_in_sql(self):
        conn, cursor = make_conn_mock()
        save_entry(conn, 1, "{}", None, datetime.now(tz=timezone.utc))
        sql = cursor.execute.call_args[0][0]
        assert "TRUE" in sql

    def test_no_diff_column_in_sql(self):
        conn, cursor = make_conn_mock()
        save_entry(conn, 1, "{}", None, datetime.now(tz=timezone.utc))
        sql = cursor.execute.call_args[0][0]
        assert "diff" not in sql


class TestSaveError:
    def test_inserts_error_and_commits(self):
        conn, cursor = make_conn_mock()
        executed_at = datetime.now(tz=timezone.utc)
        save_error(conn, 5, "something went wrong", executed_at)
        cursor.execute.assert_called_once()
        conn.commit.assert_called_once()
        params = cursor.execute.call_args[0][1]
        assert params == (5, "something went wrong", executed_at)

    def test_accepts_none_repository_id(self):
        conn, cursor = make_conn_mock()
        save_error(conn, None, "error", datetime.now(tz=timezone.utc))
        params = cursor.execute.call_args[0][1]
        assert params[0] is None


class TestCleanupOldEntries:
    def test_executes_delete_and_commits(self):
        conn, cursor = make_conn_mock()
        cleanup_old_entries(conn, 1, 15)
        cursor.execute.assert_called_once()
        conn.commit.assert_called_once()
        sql = cursor.execute.call_args[0][0]
        assert "DELETE FROM connector_rss" in sql

    def test_uses_repository_id_and_retention_days(self):
        conn, cursor = make_conn_mock()
        cleanup_old_entries(conn, 7, 30)
        params = cursor.execute.call_args[0][1]
        assert params == (7, 30)


# ---------------------------------------------------------------------------
# fetch_feed_entries
# ---------------------------------------------------------------------------


def _make_entry(id=None, title="T", link="https://example.com/1", summary="", published_parsed=None):
    """Helper to build a mock feedparser entry."""
    data = {
        "id": id,
        "title": title,
        "link": link,
        "summary": summary,
        "published_parsed": published_parsed,
    }
    mock_entry = MagicMock()
    mock_entry.get.side_effect = lambda k, d=None: data.get(k, d)
    return mock_entry


class TestFetchFeedEntries:
    @patch("fetch_rss.feedparser.parse")
    def test_returns_list_with_correct_keys(self, mock_parse):
        entry = _make_entry(id="https://example.com/entry/1", title="Hello World", summary="A summary.")
        mock_parse.return_value = MagicMock(bozo=False, entries=[entry])
        result = fetch_feed_entries("https://example.com/feed.xml")
        assert len(result) == 1
        assert result[0]["version"] == "https://example.com/entry/1"
        assert result[0]["title"] == "Hello World"
        assert result[0]["link"] == "https://example.com/1"
        assert result[0]["summary"] == "A summary."

    @patch("fetch_rss.feedparser.parse")
    def test_returns_empty_list_when_no_entries(self, mock_parse):
        mock_parse.return_value = MagicMock(bozo=False, entries=[])
        result = fetch_feed_entries("https://example.com/feed.xml")
        assert result == []

    @patch("fetch_rss.feedparser.parse")
    def test_limits_to_max_entries(self, mock_parse):
        entries = [_make_entry(id=f"guid-{i}") for i in range(10)]
        mock_parse.return_value = MagicMock(bozo=False, entries=entries)
        result = fetch_feed_entries("https://example.com/feed.xml", max_entries=3)
        assert len(result) == 3

    @patch("fetch_rss.feedparser.parse")
    def test_defaults_to_5_entries(self, mock_parse):
        entries = [_make_entry(id=f"guid-{i}") for i in range(10)]
        mock_parse.return_value = MagicMock(bozo=False, entries=entries)
        result = fetch_feed_entries("https://example.com/feed.xml")
        assert len(result) == 5

    @patch("fetch_rss.feedparser.parse")
    def test_parses_published_date(self, mock_parse):
        published_parsed = time.struct_time((2024, 6, 15, 0, 0, 0, 5, 167, 0))
        entry = _make_entry(id="guid-1", published_parsed=published_parsed)
        mock_parse.return_value = MagicMock(bozo=False, entries=[entry])
        result = fetch_feed_entries("https://example.com/feed.xml")
        assert result[0]["published"] == datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)

    @patch("fetch_rss.feedparser.parse")
    def test_handles_missing_id_falls_back_to_link(self, mock_parse):
        entry = _make_entry(id=None, link="https://example.com/fallback")
        mock_parse.return_value = MagicMock(bozo=False, entries=[entry])
        result = fetch_feed_entries("https://example.com/feed.xml")
        assert result[0]["version"] == "https://example.com/fallback"

    @patch("fetch_rss.feedparser.parse")
    def test_published_none_when_missing(self, mock_parse):
        entry = _make_entry(id="guid-1", published_parsed=None)
        mock_parse.return_value = MagicMock(bozo=False, entries=[entry])
        result = fetch_feed_entries("https://example.com/feed.xml")
        assert result[0]["published"] is None

    @patch("fetch_rss.feedparser.parse")
    def test_raises_on_bozo_with_no_entries(self, mock_parse):
        mock_feed = MagicMock(bozo=True, entries=[], bozo_exception=Exception("bad xml"))
        mock_parse.return_value = mock_feed
        import pytest

        with pytest.raises(RuntimeError, match="Feed parse error"):
            fetch_feed_entries("https://example.com/feed.xml")
