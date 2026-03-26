"""
Functional tests — require a running PostgreSQL instance.

Connection is configured via environment variables:
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
"""

import json
import os
from datetime import datetime, timezone
from unittest.mock import patch

import psycopg2
import pytest

from fetch_rss import (
    cleanup_old_entries,
    init_db,
    process_profile,
    save_entry,
    save_error,
    upsert_profile,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_conn():
    try:
        return psycopg2.connect(
            host=os.environ.get("DB_HOST", "localhost"),
            port=int(os.environ.get("DB_PORT", 5432)),
            dbname=os.environ.get("DB_NAME", "stayup"),
            user=os.environ.get("DB_USER", "stayup"),
            password=os.environ.get("DB_PASSWORD", "stayup"),
        )
    except psycopg2.OperationalError as e:
        pytest.skip(f"PostgreSQL unavailable: {e}")


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    """Create tables once for the whole test session."""
    conn = make_conn()
    init_db(conn)
    conn.close()


@pytest.fixture
def db_conn():
    """Fresh connection per test to guarantee isolation."""
    conn = make_conn()
    yield conn
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("TRUNCATE connector_rss, log, profile RESTART IDENTITY CASCADE")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------


class TestUpsertProfileFunctional:
    def test_creates_new_profile(self, db_conn):
        profile_id = upsert_profile(db_conn, "https://example.com/feed.xml")
        assert isinstance(profile_id, int)
        with db_conn.cursor() as cur:
            cur.execute("SELECT url FROM profile WHERE id = %s", (profile_id,))
            row = cur.fetchone()
        assert row[0] == "https://example.com/feed.xml"

    def test_returns_same_id_on_duplicate(self, db_conn):
        id1 = upsert_profile(db_conn, "https://example.com/feed.xml")
        id2 = upsert_profile(db_conn, "https://example.com/feed.xml")
        assert id1 == id2

    def test_different_urls_get_different_ids(self, db_conn):
        id1 = upsert_profile(db_conn, "https://example.com/feed1.xml")
        id2 = upsert_profile(db_conn, "https://example.com/feed2.xml")
        assert id1 != id2


# ---------------------------------------------------------------------------
# connector_rss
# ---------------------------------------------------------------------------


class TestSaveEntryFunctional:
    def test_row_is_persisted_with_version(self, db_conn):
        profile_id = upsert_profile(db_conn, "https://example.com/feed.xml")
        executed_at = datetime.now(tz=timezone.utc)
        content = json.dumps({"title": "Hello", "link": "https://example.com/1", "summary": "A summary."})
        save_entry(db_conn, profile_id, "guid-001", content, None, None, executed_at)

        with db_conn.cursor() as cur:
            cur.execute("SELECT version, content, success FROM connector_rss WHERE provider_id = %s", (profile_id,))
            row = cur.fetchone()
        assert row[0] == "guid-001"
        assert json.loads(row[1])["title"] == "Hello"
        assert row[2] is True

    def test_row_is_persisted_without_version(self, db_conn):
        profile_id = upsert_profile(db_conn, "https://example.com/feed.xml")
        save_entry(db_conn, profile_id, None, '{"title": "test"}', None, None, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT version, content FROM connector_rss WHERE provider_id = %s", (profile_id,))
            row = cur.fetchone()
        assert row[0] is None
        assert "test" in row[1]

    def test_datetime_stored(self, db_conn):
        profile_id = upsert_profile(db_conn, "https://example.com/feed.xml")
        entry_date = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        save_entry(db_conn, profile_id, "guid-dt", "{}", None, entry_date, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT datetime FROM connector_rss WHERE provider_id = %s", (profile_id,))
            row = cur.fetchone()
        assert row[0].replace(tzinfo=timezone.utc) == entry_date


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


class TestCleanupOldEntriesFunctional:
    def test_keeps_only_last_3(self, db_conn):
        profile_id = upsert_profile(db_conn, "https://example.com/feed.xml")
        executed_at = datetime.now(tz=timezone.utc)
        for i in range(5):
            save_entry(db_conn, profile_id, f"guid-{i}", f'{{"title": "e{i}"}}', None, None, executed_at)

        cleanup_old_entries(db_conn, profile_id)

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_rss WHERE provider_id = %s", (profile_id,))
            count = cur.fetchone()[0]
        assert count == 3

    def test_does_nothing_when_less_than_3(self, db_conn):
        profile_id = upsert_profile(db_conn, "https://example.com/feed.xml")
        save_entry(db_conn, profile_id, "guid-001", '{"title": "e1"}', None, None, datetime.now(tz=timezone.utc))

        cleanup_old_entries(db_conn, profile_id)

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_rss WHERE provider_id = %s", (profile_id,))
            count = cur.fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------


class TestSaveErrorFunctional:
    def test_error_is_persisted(self, db_conn):
        profile_id = upsert_profile(db_conn, "https://example.com/feed.xml")
        executed_at = datetime.now(tz=timezone.utc)
        save_error(db_conn, profile_id, "No entry found.", executed_at)

        with db_conn.cursor() as cur:
            cur.execute("SELECT error, profile_id FROM log WHERE profile_id = %s", (profile_id,))
            row = cur.fetchone()
        assert row[0] == "No entry found."
        assert row[1] == profile_id

    def test_error_without_profile(self, db_conn):
        save_error(db_conn, None, "feedparser network error", datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT error FROM log WHERE profile_id IS NULL")
            row = cur.fetchone()
        assert row[0] == "feedparser network error"


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @patch("fetch_rss.fetch_latest_entry")
    def test_process_profile_first_run(self, mock_fetch, db_conn):
        """First run — stores the entry with success=True and no diff."""
        mock_fetch.return_value = {
            "version": "https://example.com/entry/1",
            "title": "Hello World",
            "link": "https://example.com/entry/1",
            "summary": "A summary.",
            "published": datetime(2024, 1, 1, tzinfo=timezone.utc),
        }
        profile_id = upsert_profile(db_conn, "https://example.com/feed.xml")
        process_profile(db_conn, profile_id, "https://example.com/feed.xml", datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT version, content, diff, success FROM connector_rss WHERE provider_id = %s", (profile_id,)
            )
            row = cur.fetchone()
        assert row[0] == "https://example.com/entry/1"
        assert json.loads(row[1])["title"] == "Hello World"
        assert row[2] is None  # no diff on first run
        assert row[3] is True

    @patch("fetch_rss.fetch_latest_entry")
    def test_process_profile_no_insert_when_same_entry(self, mock_fetch, db_conn):
        """Same GUID — no new entry is inserted."""
        mock_fetch.return_value = {
            "version": "guid-001",
            "title": "My Entry",
            "link": "https://example.com/1",
            "summary": "",
            "published": None,
        }
        profile_id = upsert_profile(db_conn, "https://example.com/feed.xml")
        process_profile(db_conn, profile_id, "https://example.com/feed.xml", datetime.now(tz=timezone.utc))
        process_profile(db_conn, profile_id, "https://example.com/feed.xml", datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_rss WHERE provider_id = %s", (profile_id,))
            count = cur.fetchone()[0]
        assert count == 1

    @patch("fetch_rss.fetch_latest_entry")
    def test_process_profile_saves_new_entry(self, mock_fetch, db_conn):
        """New GUID — a new entry is inserted."""
        profile_id = upsert_profile(db_conn, "https://example.com/feed.xml")

        mock_fetch.return_value = {
            "version": "guid-001",
            "title": "First Entry",
            "link": "https://example.com/1",
            "summary": "",
            "published": None,
        }
        process_profile(db_conn, profile_id, "https://example.com/feed.xml", datetime.now(tz=timezone.utc))

        mock_fetch.return_value = {
            "version": "guid-002",
            "title": "Second Entry",
            "link": "https://example.com/2",
            "summary": "",
            "published": None,
        }
        process_profile(db_conn, profile_id, "https://example.com/feed.xml", datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT version FROM connector_rss WHERE provider_id = %s ORDER BY executed_at DESC LIMIT 1",
                (profile_id,),
            )
            row = cur.fetchone()
        assert row[0] == "guid-002"

    @patch("fetch_rss.fetch_latest_entry")
    def test_process_profile_logs_error_on_failure(self, mock_fetch, db_conn):
        """Exception — logged to the log table."""
        mock_fetch.side_effect = Exception("feedparser network error")
        profile_id = upsert_profile(db_conn, "https://example.com/feed.xml")
        process_profile(db_conn, profile_id, "https://example.com/feed.xml", datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT error FROM log WHERE profile_id = %s", (profile_id,))
            row = cur.fetchone()
        assert "feedparser network error" in row[0]

    @patch("fetch_rss.fetch_latest_entry")
    def test_process_profile_logs_error_when_no_entry(self, mock_fetch, db_conn):
        """None returned — logged to the log table."""
        mock_fetch.return_value = None
        profile_id = upsert_profile(db_conn, "https://example.com/feed.xml")
        process_profile(db_conn, profile_id, "https://example.com/feed.xml", datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT error FROM log WHERE profile_id = %s", (profile_id,))
            row = cur.fetchone()
        assert row is not None
