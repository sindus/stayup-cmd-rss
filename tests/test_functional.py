"""
Functional tests — require a running PostgreSQL instance.

Connection is configured via environment variables:
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
"""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import psycopg2
import pytest

from fetch_rss import (
    DISPLAY_TEMPLATE,
    cleanup_old_entries,
    init_db,
    process_repository,
    save_entry,
    save_error,
    upsert_repository,
)


class TestInitDb:
    def test_registers_name_and_display_template(self, db_conn):
        init_db(db_conn)
        with db_conn.cursor() as cur:
            cur.execute("SELECT display_name, sort_order, template FROM provider_registry WHERE name = 'rss'")
            display_name, sort_order, template = cur.fetchone()
        assert (display_name, sort_order) == ("RSS", 30)
        assert template == DISPLAY_TEMPLATE  # psycopg2 decodes JSONB

    def test_is_idempotent(self, db_conn):
        init_db(db_conn)
        init_db(db_conn)
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM provider_registry WHERE name = 'rss'")
            assert cur.fetchone()[0] == 1


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
        cur.execute("TRUNCATE connector_rss, log, repository RESTART IDENTITY CASCADE")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# repository
# ---------------------------------------------------------------------------


class TestUpsertRepositoryFunctional:
    def test_creates_new_repository(self, db_conn):
        repository_id = upsert_repository(db_conn, "https://example.com/feed.xml")
        assert isinstance(repository_id, int)
        with db_conn.cursor() as cur:
            cur.execute("SELECT url, type FROM repository WHERE id = %s", (repository_id,))
            row = cur.fetchone()
        assert row[0] == "https://example.com/feed.xml"
        assert row[1] == "rss"

    def test_returns_same_id_on_duplicate(self, db_conn):
        id1 = upsert_repository(db_conn, "https://example.com/feed.xml")
        id2 = upsert_repository(db_conn, "https://example.com/feed.xml")
        assert id1 == id2

    def test_different_urls_get_different_ids(self, db_conn):
        id1 = upsert_repository(db_conn, "https://example.com/feed1.xml")
        id2 = upsert_repository(db_conn, "https://example.com/feed2.xml")
        assert id1 != id2


# ---------------------------------------------------------------------------
# connector_rss
# ---------------------------------------------------------------------------


class TestSaveEntryFunctional:
    def test_row_is_persisted(self, db_conn):
        repository_id = upsert_repository(db_conn, "https://example.com/feed.xml")
        executed_at = datetime.now(tz=timezone.utc)
        import json

        content = json.dumps(
            {"version": "guid-001", "title": "Hello", "link": "https://example.com/1", "summary": "A summary."}
        )
        save_entry(db_conn, repository_id, content, None, executed_at)

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT content, success FROM connector_rss WHERE repository_id = %s",
                (repository_id,),
            )
            row = cur.fetchone()
        assert json.loads(row[0])["version"] == "guid-001"
        assert json.loads(row[0])["title"] == "Hello"
        assert row[1] is True

    def test_datetime_stored(self, db_conn):
        repository_id = upsert_repository(db_conn, "https://example.com/feed.xml")
        entry_date = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        save_entry(db_conn, repository_id, '{"version": "guid-dt"}', entry_date, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT datetime FROM connector_rss WHERE repository_id = %s", (repository_id,))
            row = cur.fetchone()
        assert row[0].replace(tzinfo=timezone.utc) == entry_date

    def test_no_diff_column(self, db_conn):
        repository_id = upsert_repository(db_conn, "https://example.com/feed.xml")
        save_entry(db_conn, repository_id, '{"version": "guid-001", "title": "t"}', None, datetime.now(tz=timezone.utc))
        with db_conn.cursor() as cur:
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'connector_rss'")
            columns = [row[0] for row in cur.fetchall()]
        assert "diff" not in columns


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


class TestCleanupOldEntriesFunctional:
    def test_deletes_entries_older_than_15_days(self, db_conn):
        repository_id = upsert_repository(db_conn, "https://example.com/feed.xml")
        old_date = datetime.now(tz=timezone.utc) - timedelta(days=16)
        recent_date = datetime.now(tz=timezone.utc)

        save_entry(db_conn, repository_id, '{"version": "guid-old", "title": "old"}', None, old_date)
        save_entry(db_conn, repository_id, '{"version": "guid-new", "title": "new"}', None, recent_date)

        cleanup_old_entries(db_conn, repository_id, 15)

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_rss WHERE repository_id = %s", (repository_id,))
            count = cur.fetchone()[0]
        assert count == 1

    def test_keeps_entries_within_15_days(self, db_conn):
        repository_id = upsert_repository(db_conn, "https://example.com/feed.xml")
        recent_date = datetime.now(tz=timezone.utc) - timedelta(days=14)
        save_entry(db_conn, repository_id, '{"version": "guid-001", "title": "e1"}', None, recent_date)

        cleanup_old_entries(db_conn, repository_id, 15)

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_rss WHERE repository_id = %s", (repository_id,))
            count = cur.fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------


class TestSaveErrorFunctional:
    def test_error_is_persisted(self, db_conn):
        repository_id = upsert_repository(db_conn, "https://example.com/feed.xml")
        executed_at = datetime.now(tz=timezone.utc)
        save_error(db_conn, repository_id, "No entry found.", executed_at)

        with db_conn.cursor() as cur:
            cur.execute("SELECT error, repository_id FROM log WHERE repository_id = %s", (repository_id,))
            row = cur.fetchone()
        assert row[0] == "No entry found."
        assert row[1] == repository_id

    def test_error_without_repository(self, db_conn):
        save_error(db_conn, None, "feedparser network error", datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT error FROM log WHERE repository_id IS NULL")
            row = cur.fetchone()
        assert row[0] == "feedparser network error"


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def _make_feed_entry(self, version, title="Title", link="https://example.com/1", summary=""):
        return {"version": version, "title": title, "link": link, "summary": summary, "published": None}

    @patch("fetch_rss.fetch_feed_entries")
    def test_first_run_stores_only_latest(self, mock_fetch, db_conn):
        """First run — stores only the most recent article."""
        mock_fetch.return_value = [
            self._make_feed_entry("guid-001", title="Latest"),
            self._make_feed_entry("guid-000", title="Older"),
        ]
        repository_id = upsert_repository(db_conn, "https://example.com/feed.xml")
        process_repository(db_conn, repository_id, "https://example.com/feed.xml", datetime.now(tz=timezone.utc), {})

        import json

        with db_conn.cursor() as cur:
            cur.execute("SELECT content FROM connector_rss WHERE repository_id = %s", (repository_id,))
            rows = cur.fetchall()
        assert len(rows) == 1
        assert json.loads(rows[0][0])["version"] == "guid-001"
        assert json.loads(rows[0][0])["title"] == "Latest"

    @patch("fetch_rss.fetch_feed_entries")
    def test_no_insert_when_same_entry(self, mock_fetch, db_conn):
        """Same GUID — no new entry is inserted."""
        mock_fetch.return_value = [self._make_feed_entry("guid-001")]
        repository_id = upsert_repository(db_conn, "https://example.com/feed.xml")
        process_repository(db_conn, repository_id, "https://example.com/feed.xml", datetime.now(tz=timezone.utc), {})
        process_repository(db_conn, repository_id, "https://example.com/feed.xml", datetime.now(tz=timezone.utc), {})

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_rss WHERE repository_id = %s", (repository_id,))
            count = cur.fetchone()[0]
        assert count == 1

    @patch("fetch_rss.fetch_feed_entries")
    def test_stores_all_new_entries_until_known(self, mock_fetch, db_conn):
        """New GUIDs — all new entries up to the known one are stored."""
        repository_id = upsert_repository(db_conn, "https://example.com/feed.xml")

        # First run: store guid-001
        mock_fetch.return_value = [self._make_feed_entry("guid-001", title="Entry 1")]
        process_repository(db_conn, repository_id, "https://example.com/feed.xml", datetime.now(tz=timezone.utc), {})

        # Second run: 2 new entries appeared (guid-003, guid-002), guid-001 is the known one
        mock_fetch.return_value = [
            self._make_feed_entry("guid-003", title="Entry 3"),
            self._make_feed_entry("guid-002", title="Entry 2"),
            self._make_feed_entry("guid-001", title="Entry 1"),
        ]
        process_repository(db_conn, repository_id, "https://example.com/feed.xml", datetime.now(tz=timezone.utc), {})

        import json

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT content FROM connector_rss WHERE repository_id = %s ORDER BY id",
                (repository_id,),
            )
            versions = [json.loads(row[0])["version"] for row in cur.fetchall()]
        assert versions == ["guid-001", "guid-003", "guid-002"]

    @patch("fetch_rss.fetch_feed_entries")
    def test_stores_at_most_max_entries_when_no_match(self, mock_fetch, db_conn):
        """No match found — stores up to MAX_NEW_ENTRIES_PER_RUN entries."""
        repository_id = upsert_repository(db_conn, "https://example.com/feed.xml")

        # First run
        mock_fetch.return_value = [self._make_feed_entry("guid-000")]
        process_repository(db_conn, repository_id, "https://example.com/feed.xml", datetime.now(tz=timezone.utc), {})

        # Second run: 5 completely new entries, old one not present
        mock_fetch.return_value = [self._make_feed_entry(f"guid-{i:03d}", title=f"Entry {i}") for i in range(5, 0, -1)]
        process_repository(db_conn, repository_id, "https://example.com/feed.xml", datetime.now(tz=timezone.utc), {})

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_rss WHERE repository_id = %s", (repository_id,))
            count = cur.fetchone()[0]
        # 1 (first run) + 5 (all new, no match found)
        assert count == 6

    @patch("fetch_rss.fetch_feed_entries")
    def test_logs_error_on_failure(self, mock_fetch, db_conn):
        """Exception — logged to the log table."""
        mock_fetch.side_effect = Exception("feedparser network error")
        repository_id = upsert_repository(db_conn, "https://example.com/feed.xml")
        process_repository(db_conn, repository_id, "https://example.com/feed.xml", datetime.now(tz=timezone.utc), {})

        with db_conn.cursor() as cur:
            cur.execute("SELECT error FROM log WHERE repository_id = %s", (repository_id,))
            row = cur.fetchone()
        assert "feedparser network error" in row[0]

    @patch("fetch_rss.fetch_feed_entries")
    def test_logs_error_when_no_entry(self, mock_fetch, db_conn):
        """Empty feed — logged to the log table."""
        mock_fetch.return_value = []
        repository_id = upsert_repository(db_conn, "https://example.com/feed.xml")
        process_repository(db_conn, repository_id, "https://example.com/feed.xml", datetime.now(tz=timezone.utc), {})

        with db_conn.cursor() as cur:
            cur.execute("SELECT error FROM log WHERE repository_id = %s", (repository_id,))
            row = cur.fetchone()
        assert row is not None
