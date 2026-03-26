# Stayup

Monitors RSS feeds and stores the latest entry in a PostgreSQL database.

For each tracked profile, the script fetches the most recent entry using feedparser. A new entry is only stored when the entry's GUID has changed since the last run. The three most recent entries per profile are kept.

## Requirements

- [Docker](https://www.docker.com/) and Docker Compose

## Setup

```bash
git clone https://github.com/sindus/stayup-cmd-rss.git
cd stayup-cmd-rss
cp .env.example .env
```

Open `.env` and configure your database connection.

### Option A — Local database (Docker)

The default values in `.env` work out of the box with the bundled `db` service. No changes needed.

### Option B — External database (Render, Railway, etc.)

Set the full connection URL in `.env`:

```env
DATABASE_URL=postgresql://user:password@host:5432/dbname
```

> **Note:** Tables are created automatically on the first run.

## Usage

**Start the database:**
```bash
docker compose up db -d
```

**Track an RSS feed:**
```bash
docker compose run --rm fetch_rss --add https://example.com/feed.xml
docker compose run --rm fetch_rss --add https://feeds.feedburner.com/example
```

**Run the script manually:**
```bash
docker compose run --rm fetch_rss
```

**Browse the database (pgAdmin):**
```bash
docker compose up pgadmin -d
```
Open [http://localhost:5050](http://localhost:5050) — credentials: `admin@admin.com` / `admin`

Connect to the server using host `db`, port `5432`, and the credentials from your `.env`.

## Automation

The script runs automatically every night at midnight UTC via GitHub Actions.

To enable it on your fork, add a `DATABASE_URL` secret in:
**Settings → Secrets and variables → Actions → New repository secret**

You can also trigger the workflow manually from the **Actions → Daily RSS fetch → Run workflow** tab.

## Development

**Install the pre-commit hook** (runs linter + tests before every commit):
```bash
cp scripts/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
```

**Run tests:**
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

## Database schema

| Table | Description |
|---|---|
| `profile` | Tracked RSS feed URLs |
| `connector_rss` | Stored entries (last 3 per profile) |
| `log` | Errors encountered during retrieval |

### `connector_rss` columns

| Column | Description |
|---|---|
| `version` | Entry GUID, falls back to link if absent |
| `content` | JSON: `{"title", "link", "summary"}` |
| `diff` | Previous entry's content, null on first run |
| `datetime` | Publication date from `entry.published_parsed` |
| `executed_at` | Timestamp when the script ran |
| `success` | Always `true` — errors are stored in the `log` table |

## Project structure

```
stayup-cmd-rss/
├── fetch_rss.py            # Main script
├── tests/
│   ├── test_unit.py        # Unit tests (no external dependencies)
│   └── test_functional.py  # Functional tests (require PostgreSQL)
├── .env.example            # Configuration template
├── docker-compose.yml
├── Dockerfile
└── pyproject.toml          # Ruff + Black configuration
```
