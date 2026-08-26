#!/usr/bin/env python3
"""
Stayup — monitors RSS feeds and stores the latest entries in PostgreSQL.

For each tracked repository of type 'rss', the script fetches the most recent
entries using feedparser. On first run the latest article is stored. On
subsequent runs all new articles (up to config["max_entries"], default 5) are stored
until the already-known entry is reached. Entries older than config["retention_days"]
(default 15 days) are cleaned up each run.
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
CREATE TABLE IF NOT EXISTS repository (
    id          SERIAL PRIMARY KEY,
    url         TEXT NOT NULL UNIQUE,
    type        TEXT NOT NULL,
    config      JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS connector_rss (
    id          SERIAL PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repository(id),
    content     TEXT NOT NULL,
    datetime    TIMESTAMPTZ,
    executed_at TIMESTAMPTZ NOT NULL,
    success     BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS log (
    id          SERIAL PRIMARY KEY,
    repository_id  INTEGER,
    error       TEXT NOT NULL,
    executed_at TIMESTAMPTZ NOT NULL
);

-- Registre partagé des providers : chaque collecteur y déclare son nom affiché au
-- démarrage. L'API stayup-api lit cette table pour construire une UI dynamique ;
-- elle ne connaît aucun nom de provider en dur, seulement les tables connector_*.
CREATE TABLE IF NOT EXISTS provider_registry (
    name          TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    sort_order    INTEGER NOT NULL DEFAULT 100,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO provider_registry (name, display_name, sort_order)
VALUES ('rss', 'RSS', 30)
ON CONFLICT (name) DO UPDATE SET display_name = EXCLUDED.display_name, updated_at = NOW();
"""


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


def upsert_repository(conn: psycopg2.extensions.connection, url: str) -> int:
    """Insert a repository URL if it does not exist yet and return its id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO repository (url, type)
            VALUES (%s, 'rss')
            ON CONFLICT (url) DO UPDATE SET url = EXCLUDED.url
            RETURNING id
            """,
            (url,),
        )
        row = cur.fetchone()
    conn.commit()
    return row[0]


def get_repositories(conn: psycopg2.extensions.connection) -> list[tuple[int, str, dict]]:
    """Return all repositories of type 'rss' as a list of (id, url, config) tuples."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, url, config FROM repository WHERE type = 'rss' ORDER BY id")
        rows = cur.fetchall()
        return [(row[0], row[1], json.loads(row[2]) if isinstance(row[2], str) else (row[2] or {})) for row in rows]


def get_latest_entry(conn: psycopg2.extensions.connection, repository_id: int) -> str | None:
    """Return (content) of the most recent successful RSS entry.

    Returns (None, None) if no entry exists yet.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT content FROM connector_rss
            WHERE repository_id = %s AND success = TRUE
            ORDER BY executed_at DESC
            LIMIT 1
            """,
            (repository_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def save_entry(
    conn: psycopg2.extensions.connection,
    repository_id: int,
    content: str,
    entry_datetime: datetime | None,
    executed_at: datetime,
) -> None:
    """Persist an RSS entry to the database."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO connector_rss (repository_id, content, datetime, executed_at, success)
            VALUES (%s, %s, %s, %s, TRUE)
            """,
            (repository_id, content, entry_datetime, executed_at),
        )
    conn.commit()


def cleanup_old_entries(conn: psycopg2.extensions.connection, repository_id: int, retention_days: int) -> None:
    """Delete connector_rss entries for a repository older than retention_days days."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM connector_rss WHERE repository_id = %s AND executed_at < NOW() - %s * INTERVAL '1 day'",
            (repository_id, retention_days),
        )
    conn.commit()


def save_error(
    conn: psycopg2.extensions.connection,
    repository_id: int | None,
    error: str,
    executed_at: datetime,
) -> None:
    """Persist a retrieval error to the log table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO log (repository_id, error, executed_at)
            VALUES (%s, %s, %s)
            """,
            (repository_id, error, executed_at),
        )
    conn.commit()


def _parse_entry(raw_entry) -> dict:
    """Extract relevant fields from a feedparser entry."""
    version = raw_entry.get("id") or raw_entry.get("link")

    published: datetime | None = None
    published_parsed = raw_entry.get("published_parsed")
    if published_parsed:
        try:
            published = datetime(*published_parsed[:6], tzinfo=timezone.utc)
        except Exception:
            pass

    return {
        "version": version,
        "title": raw_entry.get("title"),
        "link": raw_entry.get("link"),
        "summary": raw_entry.get("summary"),
        "published": published,
    }


def fetch_feed_entries(feed_url: str, max_entries: int = 5) -> list[dict]:
    """Return metadata for the most recent entries of an RSS/Atom feed.

    Returns a list of dicts (newest first) with keys: version, title, link,
    summary, published. Returns an empty list if the feed has no entries.
    feedparser.parse() never raises — it returns an empty feed on network errors.
    """
    feed = feedparser.parse(feed_url, agent="stayup-rss/1.0")
    if feed.bozo and not feed.entries:
        status = getattr(feed, "status", "no status")
        href = getattr(feed, "href", feed_url)
        raise RuntimeError(f"Feed parse error (HTTP {status}, url={href}): {feed.bozo_exception}")
    return [_parse_entry(e) for e in feed.entries[:max_entries]]


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def process_repository(
    conn: psycopg2.extensions.connection, repository_id: int, repository_url: str, executed_at: datetime, config: dict
) -> None:
    """Fetch new RSS entries for one repository and persist them.

    - If no previous entry exists, only the latest article is stored.
    - Otherwise, articles are stored from newest to oldest until the already-known
      version is reached, up to config["max_entries"] (default 5) articles.
    - Any exception is caught, logged to the `log` table, and printed to stderr.
    """
    max_entries = config.get("max_entries", 5)
    try:
        entries = fetch_feed_entries(repository_url, max_entries=max_entries)
        if not entries:
            raise RuntimeError("No entry found.")

        last_version = get_latest_entry(conn, repository_id)

        last_version = json.loads(last_version)["version"] if last_version else None

        if last_version is None:
            # First run: store only the most recent article.
            entry = entries[0]
            content = json.dumps(
                {
                    "version": entry["version"],
                    "title": entry["title"],
                    "link": entry["link"],
                    "summary": entry["summary"],
                },
                ensure_ascii=False,
            )
            save_entry(conn, repository_id, content, entry["published"], executed_at)
            return

        for entry in entries:
            if entry["version"] == last_version:
                break
            content = json.dumps(
                {
                    "version": entry["version"],
                    "title": entry["title"],
                    "link": entry["link"],
                    "summary": entry["summary"],
                },
                ensure_ascii=False,
            )
            save_entry(conn, repository_id, content, entry["published"], executed_at)

    except Exception as e:
        save_error(conn, repository_id, str(e), executed_at)
        print(f"[{repository_url}] Error: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor RSS feeds and store latest entries.")
    parser.add_argument("--add", metavar="URL", help="Add a repository to track and exit.")
    args = parser.parse_args()

    conn = get_db_conn()
    try:
        init_db(conn)

        if args.add:
            upsert_repository(conn, args.add)
            print(f"Repository added: {args.add}")
            return

        executed_at = datetime.now(tz=timezone.utc)
        repositories = get_repositories(conn)

        if not repositories:
            print("No repositories tracked. Use --add <url> to add one.")
            return

        for repository_id, repository_url, config in repositories:
            process_repository(conn, repository_id, repository_url, executed_at, config)
            cleanup_old_entries(conn, repository_id, config.get("retention_days", 15))

    finally:
        conn.close()


if __name__ == "__main__":
    main()
