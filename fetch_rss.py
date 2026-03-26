#!/usr/bin/env python3
"""
Stayup — monitors RSS feeds and stores the latest entry in PostgreSQL.

For each tracked profile, the script fetches the most recent entry using
feedparser. A new entry is only stored when the entry's GUID has changed
since the last run. The three most recent entries per profile are kept.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import feedparser
import psycopg2

DDL = """
CREATE TABLE IF NOT EXISTS profile (
    id          SERIAL PRIMARY KEY,
    url         TEXT NOT NULL UNIQUE,
    config      JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS connector_rss (
    id          SERIAL PRIMARY KEY,
    provider_id INTEGER NOT NULL REFERENCES profile(id),
    version     TEXT,
    content     TEXT NOT NULL,
    diff        TEXT,
    datetime    TIMESTAMPTZ,
    executed_at TIMESTAMPTZ NOT NULL,
    success     BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS log (
    id          SERIAL PRIMARY KEY,
    profile_id  INTEGER,
    error       TEXT NOT NULL,
    executed_at TIMESTAMPTZ NOT NULL
);
"""

# Maximum number of RSS entries kept per profile.
MAX_ENTRIES_PER_PROFILE = 3


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def get_db_conn() -> psycopg2.extensions.connection:
    """Return a psycopg2 connection.

    Reads DATABASE_URL first; falls back to individual DB_* environment
    variables (DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD).
    """
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return psycopg2.connect(database_url)
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", 5432)),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def init_db(conn: psycopg2.extensions.connection) -> None:
    """Create tables if they don't exist."""
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()


def upsert_profile(conn: psycopg2.extensions.connection, url: str) -> int:
    """Insert a profile URL if it does not exist yet and return its id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO profile (url)
            VALUES (%s)
            ON CONFLICT (url) DO UPDATE SET url = EXCLUDED.url
            RETURNING id
            """,
            (url,),
        )
        row = cur.fetchone()
    conn.commit()
    return row[0]


def get_profiles(conn: psycopg2.extensions.connection) -> list[tuple[int, str]]:
    """Return all tracked profiles as a list of (id, url) tuples."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, url FROM profile ORDER BY id")
        return cur.fetchall()


def get_latest_entry(conn: psycopg2.extensions.connection, profile_id: int) -> tuple[str | None, str | None]:
    """Return (version, content) of the most recent successful RSS entry.

    Returns (None, None) if no entry exists yet.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT version, content FROM connector_rss
            WHERE provider_id = %s AND success = TRUE
            ORDER BY executed_at DESC
            LIMIT 1
            """,
            (profile_id,),
        )
        row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def save_entry(
    conn: psycopg2.extensions.connection,
    profile_id: int,
    version: str | None,
    content: str,
    diff: str | None,
    entry_datetime: datetime | None,
    executed_at: datetime,
) -> None:
    """Persist an RSS entry to the database."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO connector_rss (provider_id, version, content, diff, datetime, executed_at, success)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
            """,
            (profile_id, version, content, diff, entry_datetime, executed_at),
        )
    conn.commit()


def cleanup_old_entries(conn: psycopg2.extensions.connection, profile_id: int) -> None:
    """Delete RSS entries beyond the MAX_ENTRIES_PER_PROFILE most recent ones."""
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM connector_rss
            WHERE provider_id = %s
              AND id NOT IN (
                SELECT id FROM connector_rss
                WHERE provider_id = %s
                ORDER BY executed_at DESC
                LIMIT %s
              )
            """,
            (profile_id, profile_id, MAX_ENTRIES_PER_PROFILE),
        )
    conn.commit()


def save_error(
    conn: psycopg2.extensions.connection,
    profile_id: int | None,
    error: str,
    executed_at: datetime,
) -> None:
    """Persist a retrieval error to the log table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO log (profile_id, error, executed_at)
            VALUES (%s, %s, %s)
            """,
            (profile_id, error, executed_at),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# RSS fetching
# ---------------------------------------------------------------------------


def fetch_latest_entry(feed_url: str) -> dict | None:
    """Return metadata for the most recent entry of an RSS/Atom feed.

    Returns a dict with keys: version, title, link, summary, published.
    Returns None if the feed has no entries.
    feedparser.parse() never raises — it returns an empty feed on network errors.
    """
    feed = feedparser.parse(feed_url, agent="stayup-rss/1.0")
    if not feed.entries:
        return None

    entry = feed.entries[0]
    version = entry.get("id") or entry.get("link")

    published: datetime | None = None
    published_parsed = entry.get("published_parsed")
    if published_parsed:
        try:
            published = datetime(*published_parsed[:6], tzinfo=timezone.utc)
        except Exception:
            pass

    return {
        "version": version,
        "title": entry.get("title"),
        "link": entry.get("link"),
        "summary": entry.get("summary"),
        "published": published,
    }


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def process_profile(
    conn: psycopg2.extensions.connection, profile_id: int, profile_url: str, executed_at: datetime
) -> None:
    """Fetch the latest RSS entry for one profile and persist it if new.

    - If no previous entry exists, the entry is stored as the initial snapshot.
    - A new entry is stored when the version (GUID) changes.
    - After saving, old entries beyond MAX_ENTRIES_PER_PROFILE are pruned.
    - Any exception is caught, logged to the `log` table, and printed to stderr.
    """
    try:
        entry = fetch_latest_entry(profile_url)
        if entry is None:
            raise RuntimeError("No entry found.")

        prev_version, prev_content = get_latest_entry(conn, profile_id)

        if prev_version == entry["version"]:
            return

        content = json.dumps(
            {
                "title": entry["title"],
                "link": entry["link"],
                "summary": entry["summary"],
            },
            ensure_ascii=False,
        )

        diff = None if prev_content is None else prev_content
        save_entry(conn, profile_id, entry["version"], content, diff, entry["published"], executed_at)
        cleanup_old_entries(conn, profile_id)

    except Exception as e:
        save_error(conn, profile_id, str(e), executed_at)
        print(f"[{profile_url}] Error: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor RSS feeds and store latest entries.")
    parser.add_argument("--add", metavar="URL", help="Add a profile to track and exit.")
    args = parser.parse_args()

    conn = get_db_conn()
    try:
        init_db(conn)

        if args.add:
            upsert_profile(conn, args.add)
            print(f"Profile added: {args.add}")
            return

        executed_at = datetime.now(tz=timezone.utc)
        profiles = get_profiles(conn)

        if not profiles:
            print("No profiles tracked. Use --add <url> to add one.")
            return

        for profile_id, profile_url in profiles:
            process_profile(conn, profile_id, profile_url, executed_at)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
