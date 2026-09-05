#!/usr/bin/env python3
"""
Stayup — monitors RSS feeds and stores the latest entries via stayup-api.

For each tracked repository of type 'rss', the script fetches the most recent
entries using feedparser. On first run the latest article is stored. On
subsequent runs all new articles (up to config["max_entries"], default 5) are stored
until the already-known entry is reached. Entries older than config["retention_days"]
(default 15 days) are cleaned up each run.

Talks to stayup-api over HTTP (STAYUP_API_URL + STAYUP_API_KEY) — it never
touches a database directly. See stayup-api/docs/self-hosting-and-providers.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import feedparser
import requests

PROVIDER_TYPE = "rss"

# Nom affiché du provider dans les apps (fallback : nom de table capitalisé).
DISPLAY_NAME = "RSS"

# Où ce connecteur se classe parmi les autres dans la barre latérale.
SORT_ORDER = 30

# Instance stayup-api à laquelle parler, et la clé qui authentifie ce
# connecteur pour le provider 'rss' — obtenue depuis l'admin de cette
# instance (voir stayup-api/docs/self-hosting-and-providers.md).
API_URL = os.environ.get("STAYUP_API_URL", "http://localhost:3000").rstrip("/")
API_KEY = os.environ.get("STAYUP_API_KEY")

DEFAULT_MAX_ENTRIES = 5
DEFAULT_RETENTION_DAYS = 15

# Manifeste d'affichage : comment les 3 apps (ui / desktop / mobile) rendent les
# lignes de ce connecteur, sans une ligne de code côté app. stayup-api le relaie
# tel quel depuis provider_registry.template, sans jamais l'interpréter.
# Schéma : voir stayup-api/docs/self-hosting-and-providers.md.
#
# Une entrée = un item de connector_item. `content` est un JSON
# {title, link, summary}, `summary` étant du HTML (mode "html").
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
        "sortOrder": SORT_ORDER,
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


# ---------------------------------------------------------------------------
# stayup-api client
# ---------------------------------------------------------------------------


def api_request(method: str, path: str, **kwargs) -> dict | None:
    """Call one of stayup-api's /connector-api/rss/* endpoints.

    Raises RuntimeError if STAYUP_API_KEY isn't set, or requests.HTTPError on
    a non-2xx response (via raise_for_status — 403 means this key isn't
    scoped to 'rss', 401 means it's missing or revoked).
    """
    if not API_KEY:
        raise RuntimeError("STAYUP_API_KEY is not set.")
    url = f"{API_URL}/connector-api/{PROVIDER_TYPE}{path}"
    headers = {"Authorization": f"Bearer {API_KEY}"}
    response = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    response.raise_for_status()
    return response.json() if response.content else None


def register_provider() -> None:
    """Auto-déclaration au démarrage — nom affiché et manifeste d'affichage."""
    api_request(
        "POST",
        "/register",
        json={
            "displayName": DISPLAY_NAME,
            "sortOrder": SORT_ORDER,
            "template": DISPLAY_TEMPLATE,
        },
    )


def add_source(url: str) -> int:
    """Track a new feed URL and return its id."""
    result = api_request("POST", "/sources", json={"url": url})
    return result["id"]


def get_sources() -> list[tuple[int, str, dict]]:
    """Return all tracked sources as (id, url, config) tuples."""
    result = api_request("GET", "/sources")
    return [(s["id"], s["url"], s.get("config") or {}) for s in result["sources"]]


def get_latest_version(repository_id: int) -> str | None:
    """Return the GUID of the most recently stored entry, or None on first run."""
    result = api_request("GET", f"/sources/{repository_id}/state")
    return result["version"]


def save_entries(repository_id: int, entries: list[dict], executed_at: datetime) -> None:
    """Persist a batch of entries in a single call. No-op on an empty batch."""
    if not entries:
        return
    items = [
        {
            "repositoryId": repository_id,
            "version": entry["version"],
            "content": json.dumps(
                {"title": entry["title"], "link": entry["link"], "summary": entry["summary"]},
                ensure_ascii=False,
            ),
            "datetime": entry["published"].isoformat() if entry["published"] else None,
            "executedAt": executed_at.isoformat(),
            "success": True,
        }
        for entry in entries
    ]
    api_request("POST", "/items", json={"items": items})


def cleanup_old_entries(repository_id: int, retention_days: int) -> None:
    """Delete stored entries for a repository older than retention_days days."""
    api_request(
        "DELETE",
        f"/sources/{repository_id}/old-items",
        params={"retentionDays": retention_days},
    )


def save_error(repository_id: int | None, error: str, executed_at: datetime) -> None:
    """Persist a retrieval error."""
    api_request(
        "POST",
        "/errors",
        json={"repositoryId": repository_id, "error": error, "executedAt": executed_at.isoformat()},
    )


def save_feed_title(repository_id: int, title: str) -> None:
    """Store the feed's channel <title> in repository.config so the apps can
    label the flux with it. The display template falls back to the URL domain
    when this key is absent. A merge, not a replace — see mergeSourceConfig."""
    api_request("PATCH", f"/sources/{repository_id}/config", json={"config": {"title": title}})


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


def process_repository(repository_id: int, repository_url: str, executed_at: datetime, config: dict) -> None:
    """Fetch new RSS entries for one repository and persist them.

    - If no previous entry exists, only the latest article is stored.
    - Otherwise, articles are stored from newest to oldest until the already-known
      version is reached, up to config["max_entries"] (default 5) articles.
    - Any exception is caught, logged via the API, and printed to stderr.
    """
    max_entries = config.get("max_entries", DEFAULT_MAX_ENTRIES)
    try:
        feed_title, entries = fetch_feed(repository_url, max_entries=max_entries)
        if not entries:
            raise RuntimeError("No entry found.")

        # Garde `repository.config.title` à jour pour le libellé du flux (le
        # template retombe sur le domaine de l'URL quand il est absent).
        if feed_title and feed_title != config.get("title"):
            save_feed_title(repository_id, feed_title)

        last_version = get_latest_version(repository_id)

        if last_version is None:
            # First run: store only the most recent article.
            save_entries(repository_id, [entries[0]], executed_at)
            return

        new_entries = []
        for entry in entries:
            if entry["version"] == last_version:
                break
            new_entries.append(entry)
        save_entries(repository_id, new_entries, executed_at)

    except Exception as e:
        save_error(repository_id, str(e), executed_at)
        print(f"[{repository_url}] Error: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor RSS feeds and store latest entries.")
    parser.add_argument("--add", metavar="URL", help="Add a repository to track and exit.")
    args = parser.parse_args()

    register_provider()

    if args.add:
        add_source(args.add)
        print(f"Repository added: {args.add}")
        return

    executed_at = datetime.now(tz=timezone.utc)
    sources = get_sources()

    if not sources:
        print("No repositories tracked. Use --add <url> to add one.")
        return

    for repository_id, repository_url, config in sources:
        process_repository(repository_id, repository_url, executed_at, config)
        cleanup_old_entries(repository_id, config.get("retention_days", DEFAULT_RETENTION_DAYS))


if __name__ == "__main__":
    main()
