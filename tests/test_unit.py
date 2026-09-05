"""Unit tests — no external dependencies. stayup-api itself is mocked
(unittest.mock.patch on `requests.request`); its actual behavior is covered
by stayup-api's own test suite. Network access (feedparser) is mocked too."""

import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from fetch_rss import (
    DISPLAY_TEMPLATE,
    add_source,
    cleanup_old_entries,
    fetch_feed,
    get_latest_version,
    get_sources,
    process_repository,
    register_provider,
    save_entries,
    save_error,
    save_feed_title,
)

# ---------------------------------------------------------------------------
# api_request helpers
# ---------------------------------------------------------------------------


def mock_response(json_body=None, status=200):
    response = MagicMock()
    response.status_code = status
    response.content = b"{}" if json_body is not None else b""
    response.json.return_value = json_body
    response.raise_for_status.return_value = None
    return response


@patch("fetch_rss.API_KEY", "test-key")
class TestRegisterProvider:
    @patch("fetch_rss.requests.request")
    def test_posts_display_name_sort_order_and_template(self, mock_request):
        mock_request.return_value = mock_response()
        register_provider()
        method, url = mock_request.call_args[0]
        assert method == "POST"
        assert url.endswith("/connector-api/rss/register")
        body = mock_request.call_args.kwargs["json"]
        assert body["displayName"] == "RSS"
        assert body["sortOrder"] == 30
        assert body["template"] == DISPLAY_TEMPLATE

    @patch("fetch_rss.requests.request")
    def test_sends_the_bearer_token(self, mock_request):
        mock_request.return_value = mock_response()
        register_provider()
        headers = mock_request.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer test-key"


class TestApiRequestWithoutKey:
    @patch("fetch_rss.API_KEY", None)
    def test_raises_when_no_api_key_is_configured(self):
        with pytest.raises(RuntimeError, match="STAYUP_API_KEY"):
            register_provider()


@patch("fetch_rss.API_KEY", "test-key")
class TestAddSource:
    @patch("fetch_rss.requests.request")
    def test_posts_the_url_and_returns_the_id(self, mock_request):
        mock_request.return_value = mock_response({"id": 7, "url": "https://example.com/feed.xml"})
        result = add_source("https://example.com/feed.xml")
        assert result == 7
        method, url = mock_request.call_args[0]
        assert method == "POST"
        assert url.endswith("/connector-api/rss/sources")
        assert mock_request.call_args.kwargs["json"] == {"url": "https://example.com/feed.xml"}


@patch("fetch_rss.API_KEY", "test-key")
class TestGetSources:
    @patch("fetch_rss.requests.request")
    def test_returns_id_url_config_tuples(self, mock_request):
        mock_request.return_value = mock_response(
            {"sources": [{"id": 1, "url": "https://a.dev/feed.xml", "config": {"max_entries": 3}}]}
        )
        result = get_sources()
        assert result == [(1, "https://a.dev/feed.xml", {"max_entries": 3})]

    @patch("fetch_rss.requests.request")
    def test_defaults_to_empty_config(self, mock_request):
        mock_request.return_value = mock_response({"sources": [{"id": 1, "url": "https://a.dev/feed.xml"}]})
        result = get_sources()
        assert result == [(1, "https://a.dev/feed.xml", {})]


@patch("fetch_rss.API_KEY", "test-key")
class TestGetLatestVersion:
    @patch("fetch_rss.requests.request")
    def test_returns_none_on_first_run(self, mock_request):
        mock_request.return_value = mock_response({"version": None})
        assert get_latest_version(1) is None

    @patch("fetch_rss.requests.request")
    def test_returns_the_version(self, mock_request):
        mock_request.return_value = mock_response({"version": "https://example.com/entry/1"})
        assert get_latest_version(1) == "https://example.com/entry/1"
        url = mock_request.call_args[0][1]
        assert url.endswith("/connector-api/rss/sources/1/state")


@patch("fetch_rss.API_KEY", "test-key")
class TestSaveEntries:
    @patch("fetch_rss.requests.request")
    def test_batches_entries_into_one_call(self, mock_request):
        mock_request.return_value = mock_response({"success": True, "count": 2})
        entries = [
            {"version": "guid-1", "title": "T1", "link": "https://x/1", "summary": "s1", "published": None},
            {"version": "guid-2", "title": "T2", "link": "https://x/2", "summary": "s2", "published": None},
        ]
        save_entries(1, entries, datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert mock_request.call_count == 1
        body = mock_request.call_args.kwargs["json"]
        assert len(body["items"]) == 2
        assert body["items"][0]["repositoryId"] == 1
        assert json.loads(body["items"][0]["content"]) == {"title": "T1", "link": "https://x/1", "summary": "s1"}
        assert body["items"][0]["success"] is True

    @patch("fetch_rss.requests.request")
    def test_does_nothing_on_an_empty_batch(self, mock_request):
        save_entries(1, [], datetime.now(tz=timezone.utc))
        mock_request.assert_not_called()

    @patch("fetch_rss.requests.request")
    def test_serializes_the_published_date(self, mock_request):
        mock_request.return_value = mock_response({"success": True})
        entry_date = datetime(2024, 6, 15, tzinfo=timezone.utc)
        save_entries(
            1,
            [{"version": "g", "title": "T", "link": "l", "summary": "s", "published": entry_date}],
            datetime.now(tz=timezone.utc),
        )
        body = mock_request.call_args.kwargs["json"]
        assert body["items"][0]["datetime"] == entry_date.isoformat()


@patch("fetch_rss.API_KEY", "test-key")
class TestSaveError:
    @patch("fetch_rss.requests.request")
    def test_posts_the_error(self, mock_request):
        mock_request.return_value = mock_response({"success": True})
        executed_at = datetime.now(tz=timezone.utc)
        save_error(5, "something went wrong", executed_at)
        body = mock_request.call_args.kwargs["json"]
        assert body == {"repositoryId": 5, "error": "something went wrong", "executedAt": executed_at.isoformat()}

    @patch("fetch_rss.requests.request")
    def test_accepts_none_repository_id(self, mock_request):
        mock_request.return_value = mock_response({"success": True})
        save_error(None, "error", datetime.now(tz=timezone.utc))
        assert mock_request.call_args.kwargs["json"]["repositoryId"] is None


@patch("fetch_rss.API_KEY", "test-key")
class TestSaveFeedTitle:
    @patch("fetch_rss.requests.request")
    def test_patches_the_config_with_the_title(self, mock_request):
        mock_request.return_value = mock_response({"success": True})
        save_feed_title(7, "Le blog de Stéphane Robert")
        method, url = mock_request.call_args[0]
        assert method == "PATCH"
        assert url.endswith("/connector-api/rss/sources/7/config")
        assert mock_request.call_args.kwargs["json"] == {"config": {"title": "Le blog de Stéphane Robert"}}


@patch("fetch_rss.API_KEY", "test-key")
class TestCleanupOldEntries:
    @patch("fetch_rss.requests.request")
    def test_sends_retention_days_as_a_query_param(self, mock_request):
        mock_request.return_value = mock_response({"success": True})
        cleanup_old_entries(7, 30)
        method, url = mock_request.call_args[0]
        assert method == "DELETE"
        assert url.endswith("/connector-api/rss/sources/7/old-items")
        assert mock_request.call_args.kwargs["params"] == {"retentionDays": 30}


class TestDisplayTemplate:
    def test_round_trips_through_json_unchanged(self):
        assert json.loads(json.dumps(DISPLAY_TEMPLATE)) == DISPLAY_TEMPLATE

    def test_ships_a_self_describing_icon(self):
        # Le connecteur fournit son icône (tracé SVG teintable), pas une clé du
        # jeu intégré des apps : un nouveau connecteur s'affiche sans toucher au code.
        icon = DISPLAY_TEMPLATE["display"]["icon"]
        assert isinstance(icon, dict)
        assert icon["paths"]
        assert all(p[:1] in ("M", "m") for p in icon["paths"])
        assert icon["viewBox"] == "0 0 24 24"

    def test_html_detail_reads_summary_from_content(self):
        assert DISPLAY_TEMPLATE["detail"]["mode"] == "html"
        assert DISPLAY_TEMPLATE["detail"]["body"] == "summary"
        assert DISPLAY_TEMPLATE["item"]["parseContentAsJson"] is True

    def test_feed_label_prefers_the_stored_channel_title_then_the_domain(self):
        label = DISPLAY_TEMPLATE["display"]["feedLabel"]
        assert isinstance(label, list)
        assert label[0] == {"path": "$source.config.title"}
        assert label[1] == {"path": "$source.url", "format": "domain"}


# ---------------------------------------------------------------------------
# fetch_feed
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


def _parsed(entries, channel_title=None, bozo=False, bozo_exception=None):
    """Mock of a feedparser.parse() result, with a stubbed channel `feed`."""
    channel = MagicMock()
    channel.get.side_effect = lambda k, d=None: (channel_title if k == "title" else d)
    return MagicMock(bozo=bozo, entries=entries, feed=channel, bozo_exception=bozo_exception)


class TestFetchFeed:
    @patch("fetch_rss.feedparser.parse")
    def test_returns_channel_title_and_entries_with_correct_keys(self, mock_parse):
        entry = _make_entry(id="https://example.com/entry/1", title="Hello World", summary="A summary.")
        mock_parse.return_value = _parsed([entry], channel_title="  My Blog  ")
        title, result = fetch_feed("https://example.com/feed.xml")
        assert title == "My Blog"  # trimmed
        assert len(result) == 1
        assert result[0]["version"] == "https://example.com/entry/1"
        assert result[0]["title"] == "Hello World"
        assert result[0]["link"] == "https://example.com/1"
        assert result[0]["summary"] == "A summary."

    @patch("fetch_rss.feedparser.parse")
    def test_title_is_none_when_the_feed_has_no_title(self, mock_parse):
        mock_parse.return_value = _parsed([_make_entry(id="g1")], channel_title=None)
        title, _ = fetch_feed("https://example.com/feed.xml")
        assert title is None

    @patch("fetch_rss.feedparser.parse")
    def test_returns_empty_list_when_no_entries(self, mock_parse):
        mock_parse.return_value = _parsed([])
        _, result = fetch_feed("https://example.com/feed.xml")
        assert result == []

    @patch("fetch_rss.feedparser.parse")
    def test_limits_to_max_entries(self, mock_parse):
        entries = [_make_entry(id=f"guid-{i}") for i in range(10)]
        mock_parse.return_value = _parsed(entries)
        _, result = fetch_feed("https://example.com/feed.xml", max_entries=3)
        assert len(result) == 3

    @patch("fetch_rss.feedparser.parse")
    def test_defaults_to_5_entries(self, mock_parse):
        entries = [_make_entry(id=f"guid-{i}") for i in range(10)]
        mock_parse.return_value = _parsed(entries)
        _, result = fetch_feed("https://example.com/feed.xml")
        assert len(result) == 5

    @patch("fetch_rss.feedparser.parse")
    def test_parses_published_date(self, mock_parse):
        published_parsed = time.struct_time((2024, 6, 15, 0, 0, 0, 5, 167, 0))
        entry = _make_entry(id="guid-1", published_parsed=published_parsed)
        mock_parse.return_value = _parsed([entry])
        _, result = fetch_feed("https://example.com/feed.xml")
        assert result[0]["published"] == datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)

    @patch("fetch_rss.feedparser.parse")
    def test_handles_missing_id_falls_back_to_link(self, mock_parse):
        entry = _make_entry(id=None, link="https://example.com/fallback")
        mock_parse.return_value = _parsed([entry])
        _, result = fetch_feed("https://example.com/feed.xml")
        assert result[0]["version"] == "https://example.com/fallback"

    @patch("fetch_rss.feedparser.parse")
    def test_published_none_when_missing(self, mock_parse):
        entry = _make_entry(id="guid-1", published_parsed=None)
        mock_parse.return_value = _parsed([entry])
        _, result = fetch_feed("https://example.com/feed.xml")
        assert result[0]["published"] is None

    @patch("fetch_rss.feedparser.parse")
    def test_raises_on_bozo_with_no_entries(self, mock_parse):
        mock_parse.return_value = _parsed([], bozo=True, bozo_exception=Exception("bad xml"))
        with pytest.raises(RuntimeError, match="Feed parse error"):
            fetch_feed("https://example.com/feed.xml")


# ---------------------------------------------------------------------------
# process_repository — end to end, stayup-api mocked
# ---------------------------------------------------------------------------


@patch("fetch_rss.API_KEY", "test-key")
class TestProcessRepository:
    @patch("fetch_rss.save_error")
    @patch("fetch_rss.save_entries")
    @patch("fetch_rss.get_latest_version")
    @patch("fetch_rss.fetch_feed")
    def test_first_run_stores_only_latest(self, mock_fetch, mock_get_latest, mock_save, mock_save_error):
        mock_fetch.return_value = (
            None,
            [
                {"version": "guid-001", "title": "Latest", "link": "l1", "summary": "", "published": None},
                {"version": "guid-000", "title": "Older", "link": "l0", "summary": "", "published": None},
            ],
        )
        mock_get_latest.return_value = None
        executed_at = datetime.now(tz=timezone.utc)
        process_repository(1, "https://example.com/feed.xml", executed_at, {})

        mock_save.assert_called_once()
        _, entries, _ = mock_save.call_args[0]
        assert len(entries) == 1
        assert entries[0]["version"] == "guid-001"
        mock_save_error.assert_not_called()

    @patch("fetch_rss.save_error")
    @patch("fetch_rss.save_entries")
    @patch("fetch_rss.get_latest_version")
    @patch("fetch_rss.fetch_feed")
    def test_no_new_entries_when_the_known_version_is_first(self, mock_fetch, mock_get_latest, mock_save, _err):
        mock_fetch.return_value = (
            None,
            [{"version": "guid-001", "title": "T", "link": "l", "summary": "", "published": None}],
        )
        mock_get_latest.return_value = "guid-001"
        process_repository(1, "https://example.com/feed.xml", datetime.now(tz=timezone.utc), {})

        _, entries, _ = mock_save.call_args[0]
        assert entries == []

    @patch("fetch_rss.save_error")
    @patch("fetch_rss.save_entries")
    @patch("fetch_rss.get_latest_version")
    @patch("fetch_rss.fetch_feed")
    def test_stores_all_new_entries_until_the_known_one(self, mock_fetch, mock_get_latest, mock_save, _err):
        mock_fetch.return_value = (
            None,
            [
                {"version": "guid-003", "title": "3", "link": "l", "summary": "", "published": None},
                {"version": "guid-002", "title": "2", "link": "l", "summary": "", "published": None},
                {"version": "guid-001", "title": "1", "link": "l", "summary": "", "published": None},
            ],
        )
        mock_get_latest.return_value = "guid-001"
        process_repository(1, "https://example.com/feed.xml", datetime.now(tz=timezone.utc), {})

        _, entries, _ = mock_save.call_args[0]
        assert [e["version"] for e in entries] == ["guid-003", "guid-002"]

    @patch("fetch_rss.save_error")
    @patch("fetch_rss.save_entries")
    @patch("fetch_rss.get_latest_version")
    @patch("fetch_rss.fetch_feed")
    def test_stores_all_when_the_known_version_is_absent(self, mock_fetch, mock_get_latest, mock_save, _err):
        mock_fetch.return_value = (
            None,
            [
                {"version": f"guid-{i}", "title": str(i), "link": "l", "summary": "", "published": None}
                for i in range(5)
            ],
        )
        mock_get_latest.return_value = "guid-not-found"
        process_repository(1, "https://example.com/feed.xml", datetime.now(tz=timezone.utc), {})

        _, entries, _ = mock_save.call_args[0]
        assert len(entries) == 5

    @patch("fetch_rss.save_error")
    @patch("fetch_rss.fetch_feed")
    def test_logs_error_on_failure(self, mock_fetch, mock_save_error):
        mock_fetch.side_effect = Exception("feedparser network error")
        executed_at = datetime.now(tz=timezone.utc)
        process_repository(1, "https://example.com/feed.xml", executed_at, {})

        mock_save_error.assert_called_once_with(1, "feedparser network error", executed_at)

    @patch("fetch_rss.save_error")
    @patch("fetch_rss.fetch_feed")
    def test_logs_error_when_no_entry(self, mock_fetch, mock_save_error):
        mock_fetch.return_value = (None, [])
        process_repository(1, "https://example.com/feed.xml", datetime.now(tz=timezone.utc), {})
        mock_save_error.assert_called_once()

    @patch("fetch_rss.save_error")
    @patch("fetch_rss.save_entries")
    @patch("fetch_rss.get_latest_version")
    @patch("fetch_rss.save_feed_title")
    @patch("fetch_rss.fetch_feed")
    def test_stores_the_feed_channel_title(self, mock_fetch, mock_save_title, mock_get_latest, _save, _err):
        mock_fetch.return_value = (
            "Le blog de Stéphane Robert",
            [{"version": "g", "title": "T", "link": "l", "summary": "", "published": None}],
        )
        mock_get_latest.return_value = None
        process_repository(7, "https://example.com/feed.xml", datetime.now(tz=timezone.utc), {})
        mock_save_title.assert_called_once_with(7, "Le blog de Stéphane Robert")

    @patch("fetch_rss.save_error")
    @patch("fetch_rss.save_entries")
    @patch("fetch_rss.get_latest_version")
    @patch("fetch_rss.save_feed_title")
    @patch("fetch_rss.fetch_feed")
    def test_does_not_patch_config_when_the_title_is_unchanged(
        self, mock_fetch, mock_save_title, mock_get_latest, _save, _err
    ):
        mock_fetch.return_value = (
            "Same Title",
            [{"version": "g", "title": "T", "link": "l", "summary": "", "published": None}],
        )
        mock_get_latest.return_value = None
        process_repository(7, "https://example.com/feed.xml", datetime.now(tz=timezone.utc), {"title": "Same Title"})
        mock_save_title.assert_not_called()
