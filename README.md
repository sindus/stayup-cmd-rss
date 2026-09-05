# Stayup

[![CI](https://github.com/stayup-app/stayup-cmd-rss/actions/workflows/ci.yml/badge.svg)](https://github.com/stayup-app/stayup-cmd-rss/actions/workflows/ci.yml)
[![Daily RSS fetch](https://github.com/stayup-app/stayup-cmd-rss/actions/workflows/daily.yml/badge.svg)](https://github.com/stayup-app/stayup-cmd-rss/actions/workflows/daily.yml)

**Website:** https://stayup-ui.vercel.app

Monitors RSS feeds and stores the latest entries via [stayup-api](https://github.com/stayup-app/stayup-api) — this script never touches a database directly, it only calls `stayup-api`'s `/connector-api/rss/*` endpoints.

For each tracked feed, the script fetches the most recent entries using feedparser. A new entry is only stored when its GUID has changed since the last run, up to `max_entries` (default 5) per run. Entries older than `retention_days` (default 15) are cleaned up each run.

## Requirements

- Python 3.13, or [Docker](https://www.docker.com/)
- A `stayup-api` instance (the public one, or your own — see [self-hosting-and-providers.md](https://github.com/stayup-app/stayup-api/blob/main/docs/self-hosting-and-providers.md))
- An API key for the `rss` provider, created from that instance's admin panel (Connector keys → New key, provider `rss`). The key is shown once — copy it right away.

## Setup

```bash
git clone https://github.com/stayup-app/stayup-cmd-rss.git
cd stayup-cmd-rss
cp .env.example .env
```

Open `.env` and set `STAYUP_API_URL` (your `stayup-api` instance) and `STAYUP_API_KEY` (the key you created for `rss`).

> **Note:** the provider registers itself automatically on every run — nothing to create by hand beyond the key.

## Usage

**Track an RSS feed:**
```bash
docker compose run --rm fetch_rss --add https://example.com/feed.xml
docker compose run --rm fetch_rss --add https://feeds.feedburner.com/example
```

**Run the script manually:**
```bash
docker compose run --rm fetch_rss
```

Without Docker:
```bash
pip install -r requirements.txt
STAYUP_API_URL=... STAYUP_API_KEY=... python fetch_rss.py
```

## Automation

The script runs automatically every night at midnight UTC via GitHub Actions.

To enable it on your fork, add `STAYUP_API_URL` and `STAYUP_API_KEY` secrets in:
**Settings → Secrets and variables → Actions → New repository secret**

You can also trigger the workflow manually from the **Actions → Daily RSS fetch → Run workflow** tab.

## Development

**Install the pre-commit hook** (runs linter + tests before every commit):
```bash
cp scripts/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
```

**Run tests** (no external dependencies — `stayup-api` and network calls are mocked):
```bash
docker compose run --rm test
```

**Check linting:**
```bash
docker compose run --rm --entrypoint="" test sh -c "ruff check . && black --check ."
```

**Auto-format code:**
```bash
docker run --rm --entrypoint="" -v $(pwd):/app -w /app stayup-test black .
```

## What gets stored

Each stored entry is a JSON `content` blob `{"title", "link", "summary"}` (`summary` is HTML), keyed by GUID (`version`, falls back to the entry's link). The channel's `<title>` is kept in the tracked feed's config so the apps can label it — see `stayup-api`'s `connector-api` docs for the full contract.

## Project structure

```
stayup-cmd-rss/
├── fetch_rss.py       # Main script
├── tests/
│   └── test_unit.py   # Tests — stayup-api and network calls are mocked
├── .env.example       # Configuration template
├── docker-compose.yml
├── Dockerfile
└── pyproject.toml     # Ruff + Black configuration
```
