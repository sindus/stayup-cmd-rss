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

-- Registre partagé des providers : chaque collecteur y déclare son nom affiché et
-- son template d'affichage au démarrage. L'API stayup-api lit cette table pour
-- construire une UI dynamique ; elle ne connaît aucun nom de provider en dur,
-- seulement les tables connector_*. Le registre est renseigné juste après ce DDL
-- (voir REGISTER_PROVIDER_SQL) — pas ici, pour passer le template en paramètre.
CREATE TABLE IF NOT EXISTS provider_registry (
    name          TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    sort_order    INTEGER NOT NULL DEFAULT 100,
    template      JSONB,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Registre antérieur à la colonne `template` : on l'ajoute sans rien réécrire.
ALTER TABLE provider_registry ADD COLUMN IF NOT EXISTS template JSONB;
"""

PROVIDER_TYPE = "rss"

# Nom affiché du provider dans les apps (fallback : nom de table capitalisé).
DISPLAY_NAME = "RSS"

# Manifeste d'affichage : comment les 3 apps (ui / desktop / mobile) rendent les
# lignes de ce connecteur, sans une ligne de code côté app. stayup-api le relaie
# tel quel depuis provider_registry.template, sans jamais l'interpréter.
# Schéma : voir stayup-api/docs/self-hosting-and-providers.md.
#
# Une ligne connector_rss = une entrée de flux. `content` est un JSON
# {version, title, link, summary}, `summary` étant du HTML (mode "html").
DISPLAY_TEMPLATE = {
    "version": 1,
    "display": {
        "name": DISPLAY_NAME,
        # Icône auto-descriptive (tracé SVG teintable). Ondes RSS + point.
        "icon": {
            "paths": [
                "M4 11a9 9 0 0 1 9 9",
                "M4 4a16 16 0 0 1 16 16",
                "M6 19a1 1 0 0 1-2 0 1 1 0 0 1 2 0z",
            ],
            "viewBox": "0 0 24 24",
            "stroke": True,
        },
        "accent": "#a8d4b5",
        "sortOrder": 30,
        # Libellé du flux : le titre du canal quand le collecteur l'a stocké
        # (repository.config.title), sinon le domaine de l'URL.
        "feedLabel": [
            {"path": "$source.config.title"},
            {"path": "$source.url", "format": "domain"},
        ],
    },
    "item": {
        "parseContentAsJson": True,
        "fields": {
            "title": "title",
            "subtitle": {"path": "link", "format": "hostname"},
            "summary": "summary",
            "url": "link",
            "timestamp": "$row.datetime",
        },
    },
    "list": {
        "layout": "row",
        "primary": "title",
        "secondary": "subtitle",
        "meta": "timestamp",
    },
    "detail": {
        "mode": "html",
        "title": "title",
        "subtitle": {"path": "link", "format": "hostname"},
        "body": "summary",
        "openUrl": "link",
        "openLabel": "Read article",
    },
    "form": {
        "label": "RSS/Atom feed URL",
        "placeholder": "https://blog.example.com/feed.xml",
        "pattern": r"^https?://.+",
        "transform": {"trim": True},
    },
}

# Upsert du registre, template passé en paramètre (le JSON contient des guillemets
# et échapperait mal dans un DDL littéral). `sort_order` n'est pas réécrit sur
# conflit, par cohérence avec les autres collecteurs stayup-cmd-*.
REGISTER_PROVIDER_SQL = """
INSERT INTO provider_registry (name, display_name, sort_order, template)
VALUES (%s, %s, %s, %s::jsonb)
ON CONFLICT (name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    template     = EXCLUDED.template,
    updated_at   = NOW();
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
    """Create tables if they don't exist and register the provider (name + display template)."""
    with conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute(
            REGISTER_PROVIDER_SQL,
            (PROVIDER_TYPE, DISPLAY_NAME, 30, json.dumps(DISPLAY_TEMPLATE)),
        )
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


def save_feed_title(conn: psycopg2.extensions.connection, repository_id: int, title: str) -> None:
    """Store the feed's channel <title> in repository.config so the apps can
    label the flux with it. The display template falls back to the URL domain
    when this key is absent."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE repository SET config = config || jsonb_build_object('title', %s::text) WHERE id = %s",
            (title, repository_id),
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


def fetch_feed(feed_url: str, max_entries: int = 5) -> tuple[str | None, list[dict]]:
    """Return the feed's channel title and its most recent entries.

    Entries are dicts (newest first) with keys: version, title, link, summary,
    published — empty list when the feed has none. Title is ``None`` when the
    feed carries no ``<title>``. feedparser.parse() never raises — it returns an
    empty feed on network errors.
    """
    feed = feedparser.parse(feed_url, agent="stayup-rss/1.0")
    if feed.bozo and not feed.entries:
        status = getattr(feed, "status", "no status")
        href = getattr(feed, "href", feed_url)
        raise RuntimeError(f"Feed parse error (HTTP {status}, url={href}): {feed.bozo_exception}")
    title = (feed.feed.get("title") or "").strip() or None
    return title, [_parse_entry(e) for e in feed.entries[:max_entries]]


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
        feed_title, entries = fetch_feed(repository_url, max_entries=max_entries)
        if not entries:
            raise RuntimeError("No entry found.")

        # Garde `repository.config.title` à jour pour le libellé du flux (le
        # template retombe sur le domaine de l'URL quand il est absent).
        if feed_title and feed_title != config.get("title"):
            save_feed_title(conn, repository_id, feed_title)

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
