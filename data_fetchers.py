"""
Data fetchers for the Macroeconomic Real-Time Dashboard.

Classes:
    FREDFetcher        – pulls indicator data from the FRED API
    FedWatchFetcher    – scrapes / infers CME FedWatch rate probabilities
    TruthSocialFetcher – fetches posts from Truth Social
    DataCache          – thread-safe TTL cache wrapping all fetchers
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import feedparser
import requests
from bs4 import BeautifulSoup
from cachetools import TTLCache
from dateutil import parser as dateutil_parser

import config

# Directory containing cached fallback data (JSON snapshots)
_CACHE_DIR = Path(__file__).parent / "cache"

logger = logging.getLogger(__name__)

# Common HTTP headers
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ---------------------------------------------------------------------------
# FRED Fetcher
# ---------------------------------------------------------------------------

class FREDFetcher:
    """Fetch economic indicator series from the FRED API."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or config.FRED_API_KEY
        self.base_url = config.FRED_BASE_URL
        self.session = requests.Session()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @property
    def is_configured(self) -> bool:
        return bool(self.api_key) and self.api_key != "YOUR_FRED_API_KEY_HERE"

    def _safe_float(self, value: str) -> float | None:
        """Convert FRED string value to float, returning None for missing."""
        try:
            if value in (".", ""):
                return None
            return float(value)
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def fetch_series(self, series_id: str, limit: int = 12) -> list[dict]:
        """Return the latest *limit* observations for a FRED series.

        Each element: {"date": "YYYY-MM-DD", "value": float | None}
        """
        if not self.is_configured:
            raise RuntimeError(
                "FRED API key is not configured. "
                "Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html "
                "and set FRED_API_KEY in config.py or as an environment variable."
            )
        params = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": limit,
        }
        resp = self.session.get(self.base_url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        observations = data.get("observations", [])
        result = []
        for obs in observations:
            result.append({
                "date": obs.get("date"),
                "value": self._safe_float(obs.get("value", ".")),
            })
        return result

    def _build_indicator(
        self, series_id: str, name: str, observations: list[dict],
        display_mode: str = "raw",
    ) -> dict:
        """Build a rich indicator dict from raw observations.

        display_mode controls how the headline value is derived:
          "raw"      – current observation value as-is
          "yoy_pct"  – year-over-year % change (for price indices)
          "mom_diff"  – month-over-month absolute difference
          "mom_pct"  – month-over-month % change
        """
        # Filter out None values for calculations
        valid = [o for o in observations if o["value"] is not None]
        current_raw = valid[0]["value"] if valid else None
        previous_raw = valid[1]["value"] if len(valid) > 1 else None

        # Year-over-year values (comparing current with ~12 obs ago)
        year_ago_raw = valid[11]["value"] if len(valid) >= 12 else None
        yoy_change = None
        yoy_change_pct = None
        if current_raw is not None and year_ago_raw is not None and year_ago_raw != 0:
            yoy_change = round(current_raw - year_ago_raw, 4)
            yoy_change_pct = round((yoy_change / abs(year_ago_raw)) * 100, 2)

        # Month-over-month values
        mom_diff = None
        mom_pct = None
        if current_raw is not None and previous_raw is not None:
            mom_diff = round(current_raw - previous_raw, 4)
            if previous_raw != 0:
                mom_pct = round((mom_diff / abs(previous_raw)) * 100, 2)

        # Derive the headline "current" and "previous" based on display_mode
        if display_mode == "yoy_pct":
            # Headline = YoY% change; "previous" = prior month's YoY%
            display_current = yoy_change_pct
            # Calculate previous month's YoY% (valid[1] vs valid[12])
            prev_yoy = None
            if len(valid) >= 13 and valid[1]["value"] is not None and valid[12]["value"] is not None:
                two_years_ago = valid[12]["value"]
                if two_years_ago != 0:
                    prev_yoy = round(
                        ((valid[1]["value"] - two_years_ago) / abs(two_years_ago)) * 100, 2
                    )
            display_previous = prev_yoy
            # Build history as YoY% for each observation that has a 12-month-ago pair
            history = []
            for i in range(min(len(valid), 12)):
                if i + 11 < len(valid) and valid[i + 11]["value"] not in (None, 0):
                    pct = round(
                        ((valid[i]["value"] - valid[i + 11]["value"]) / abs(valid[i + 11]["value"])) * 100, 2
                    )
                    history.append({"date": valid[i]["date"], "value": pct})
            history.reverse()  # chronological order
        elif display_mode == "mom_diff":
            # Headline = MoM absolute change (e.g. NFP +256K)
            display_current = mom_diff
            # Previous = prior month's MoM diff
            prev_mom = None
            if len(valid) >= 3 and valid[1]["value"] is not None and valid[2]["value"] is not None:
                prev_mom = round(valid[1]["value"] - valid[2]["value"], 4)
            display_previous = prev_mom
            # History as MoM diffs
            history = []
            for i in range(min(len(valid) - 1, 12)):
                if valid[i]["value"] is not None and valid[i + 1]["value"] is not None:
                    diff = round(valid[i]["value"] - valid[i + 1]["value"], 4)
                    history.append({"date": valid[i]["date"], "value": diff})
            history.reverse()
        elif display_mode == "mom_pct":
            # Headline = MoM% change
            display_current = mom_pct
            prev_mom_pct = None
            if len(valid) >= 3 and valid[1]["value"] is not None and valid[2]["value"] is not None:
                if valid[2]["value"] != 0:
                    prev_mom_pct = round(
                        ((valid[1]["value"] - valid[2]["value"]) / abs(valid[2]["value"])) * 100, 2
                    )
            display_previous = prev_mom_pct
            history = []
            for i in range(min(len(valid) - 1, 12)):
                if valid[i]["value"] is not None and valid[i + 1]["value"] is not None and valid[i + 1]["value"] != 0:
                    pct = round(
                        ((valid[i]["value"] - valid[i + 1]["value"]) / abs(valid[i + 1]["value"])) * 100, 2
                    )
                    history.append({"date": valid[i]["date"], "value": pct})
            history.reverse()
        else:  # "raw"
            display_current = current_raw
            display_previous = previous_raw
            history = list(reversed(observations))  # chronological

        # Compute change between display values for the card's delta indicator
        display_change = None
        if display_current is not None and display_previous is not None:
            display_change = round(display_current - display_previous, 4)

        return {
            "series_id": series_id,
            "name": name,
            "display_mode": display_mode,
            "current": display_current,
            "previous": display_previous,
            "change": display_change,
            "raw_current": current_raw,
            "raw_previous": previous_raw,
            "yoy_change": yoy_change,
            "yoy_change_pct": yoy_change_pct,
            "mom_diff": mom_diff,
            "mom_pct": mom_pct,
            "release_date": valid[0]["date"] if valid else None,
            "history": history,
        }

    def fetch_all_macro(self) -> dict[str, dict[str, Any]]:
        """Fetch every configured FRED series grouped by category.

        Returns:
            {category: {indicator_name: {<indicator dict>}}}
        """
        if not self.is_configured:
            return {
                "_error": (
                    "FRED API key is not configured. "
                    "Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html "
                    "and set FRED_API_KEY in config.py or as an environment variable."
                )
            }

        result: dict[str, dict[str, Any]] = {}
        for category, series_map in config.FRED_SERIES.items():
            cat_data: dict[str, Any] = {}
            for series_id, (name, display_mode) in series_map.items():
                # For YoY calculations we need at least 13 months of data
                limit = 24 if display_mode == "yoy_pct" else 14
                try:
                    obs = self.fetch_series(series_id, limit=limit)
                    cat_data[name] = self._build_indicator(
                        series_id, name, obs, display_mode=display_mode
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to fetch FRED series %s: %s", series_id, exc
                    )
                    cat_data[name] = {
                        "series_id": series_id,
                        "name": name,
                        "error": str(exc),
                    }
            result[category] = cat_data
        return result

class FedWatchFetcher:
    """Attempt to retrieve CME FedWatch rate-probability data.

    Approach 1: Try the CME JSON services API (works from residential IPs).
    Approach 2: Try Playwright headless browser (optional dependency).
    Fallback:   Return FOMC dates + direct link to CME FedWatch page.
    """

    CME_FEDWATCH_URL = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"
    CME_PROBABILITIES_API = "https://www.cmegroup.com/services/fed-funds-probabilities/probabilities.json"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)

    # ------------------------------------------------------------------
    def fetch_fomc_meetings(self) -> list[dict]:
        """Return upcoming FOMC meeting dates (hardcoded fallback)."""
        now = datetime.now(timezone.utc).date()
        meetings: list[dict] = []
        for start, end in config.FOMC_DATES_2025 + config.FOMC_DATES_2026:
            end_date = datetime.strptime(end, "%Y-%m-%d").date()
            if end_date >= now:
                meetings.append({"start": start, "end": end})
        return meetings

    # ------------------------------------------------------------------
    def fetch_rate_probabilities(self) -> dict[str, Any]:
        """Try multiple approaches to get FedWatch data."""
        meetings = self.fetch_fomc_meetings()
        base_result: dict[str, Any] = {
            "meetings": meetings,
            "current_rate": "3.50% - 3.75%",  # Updated via FedWatch data
            "probabilities": None,
            "source_note": None,
        }

        # Approach 1: CME JSON API (works from residential IPs)
        prob = self._try_cme_api()
        if prob:
            base_result["probabilities"] = prob.get("probabilities")
            if prob.get("current_rate"):
                base_result["current_rate"] = prob["current_rate"]
            base_result["source_note"] = "Data from CME FedWatch API. Verify at: " + self.CME_FEDWATCH_URL
            return base_result

        # Approach 2: Playwright (optional dependency)
        prob = self._try_playwright()
        if prob:
            base_result["probabilities"] = prob.get("probabilities")
            if prob.get("current_rate"):
                base_result["current_rate"] = prob["current_rate"]
            base_result["source_note"] = "Data scraped via browser. Verify at: " + self.CME_FEDWATCH_URL
            return base_result

        # Approach 3: Load from cached JSON snapshot
        cached = self._try_cached_fallback()
        if cached:
            base_result["probabilities"] = cached.get("probabilities")
            if cached.get("current_rate"):
                base_result["current_rate"] = cached["current_rate"]
            if cached.get("meetings"):
                base_result["meetings"] = cached["meetings"]
            base_result["source_note"] = cached.get(
                "source_note",
                "Data from cached snapshot. Verify at: " + self.CME_FEDWATCH_URL,
            )
            return base_result

        # All approaches failed: return meetings + helpful link
        base_result["source_note"] = (
            "Unable to fetch live FedWatch data. This typically happens when running "
            "from a cloud/VPN IP that CME blocks. If running locally on a residential "
            "connection, the data should load automatically. Otherwise, check the "
            "CME FedWatch tool directly at: " + self.CME_FEDWATCH_URL
        )
        return base_result

    # ------------------------------------------------------------------
    def _try_cached_fallback(self) -> dict | None:
        """Load FedWatch data from a local JSON cache file."""
        cache_file = _CACHE_DIR / "fedwatch.json"
        try:
            if cache_file.exists():
                with open(cache_file) as f:
                    data = json.load(f)
                logger.info("Loaded FedWatch data from cached fallback")
                return data
        except Exception as exc:
            logger.info("Failed to load FedWatch cache: %s", exc)
        return None

    # ------------------------------------------------------------------
    def _try_cme_api(self) -> dict | None:
        """Try the CME services JSON API."""
        try:
            resp = self.session.get(self.CME_PROBABILITIES_API, timeout=15)
            if resp.status_code != 200:
                logger.info("CME API returned %s", resp.status_code)
                return None
            data = resp.json()
            return self._parse_cme_json(data)
        except Exception as exc:
            logger.info("CME API fetch failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    def _parse_cme_json(self, data: Any) -> dict | None:
        """Parse the CME probabilities JSON response."""
        if not data:
            return None
        # The CME JSON format varies; try common structures
        result = {"probabilities": [], "current_rate": None}

        # Try to extract from common CME response shapes
        if isinstance(data, dict):
            # Look for meeting-level probability data
            meetings_data = data.get("meetings") or data.get("data") or []
            if isinstance(meetings_data, list):
                for meeting in meetings_data:
                    if isinstance(meeting, dict):
                        meeting_date = meeting.get("meetingDate") or meeting.get("date", "")
                        probs = meeting.get("probabilities") or meeting.get("targetRates") or {}
                        if probs:
                            result["probabilities"].append({
                                "meeting_date": meeting_date,
                                "rates": probs,
                            })
            # Try top-level current rate
            result["current_rate"] = data.get("currentRate") or data.get("current_rate")

        return result if result["probabilities"] else None

    # ------------------------------------------------------------------
    def _try_playwright(self) -> dict | None:
        """Try using Playwright to scrape the FedWatch page (optional)."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.info("Playwright not installed -- skipping browser-based FedWatch fetch. "
                        "Install with: pip install playwright && playwright install chromium")
            return None

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(self.CME_FEDWATCH_URL, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(5000)  # extra time for QuikStrike widget

                # Extract probability text from the rendered page
                content = page.content()
                browser.close()

                soup = BeautifulSoup(content, "html.parser")
                tables = soup.find_all("table")
                if tables:
                    parsed = self._parse_probability_table(tables[0])
                    if parsed:
                        return {"probabilities": parsed, "current_rate": None}
                return None
        except Exception as exc:
            logger.info("Playwright FedWatch scrape failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    def _parse_probability_table(self, table_tag) -> list[dict] | None:
        """Best-effort parse of an HTML probability table."""
        rows = table_tag.find_all("tr")
        if len(rows) < 2:
            return None

        headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
        parsed: list[dict] = []
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cells) >= 2:
                entry: dict[str, Any] = {}
                for idx, hdr in enumerate(headers):
                    if idx < len(cells):
                        entry[hdr] = cells[idx]
                parsed.append(entry)
        return parsed if parsed else None


class TruthSocialFetcher:
    """Fetch latest posts from Truth Social via Mastodon-compatible API.

    Truth Social is a Mastodon fork, so it exposes the standard Mastodon
    REST API. We use the account lookup + statuses endpoints.

    Approach 1: Mastodon API (works from residential IPs).
    Approach 2: RSS feed (may work from some IPs).
    Approach 3: Playwright headless browser (optional dependency).
    Fallback:   Helpful error message with direct link.
    """

    API_BASE = "https://truthsocial.com"
    ACCOUNT_LOOKUP = "/api/v1/accounts/lookup"
    ACCOUNT_STATUSES = "/api/v1/accounts/{account_id}/statuses"
    # Known account ID for @realDonaldTrump (stable across requests)
    KNOWN_ACCOUNTS = {
        "realDonaldTrump": "107780257626128497",
    }
    RSS_URL_TEMPLATE = "https://truthsocial.com/@{username}.rss"
    PROFILE_URL_TEMPLATE = "https://truthsocial.com/@{username}"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)
        # Use a more browser-like Accept header for the API
        self.api_headers = {
            **_HEADERS,
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    def fetch_latest_posts(
        self, username: str = "realDonaldTrump", count: int = 20
    ) -> list[dict] | dict:
        """Try multiple approaches to get posts. Returns list or error dict."""
        # Approach 1: Mastodon API (best -- structured JSON)
        posts = self._try_mastodon_api(username, count)
        if posts:
            return posts

        # Approach 2: RSS feed
        posts = self._try_rss(username, count)
        if posts:
            return posts

        # Approach 3: Playwright (optional)
        posts = self._try_playwright(username, count)
        if posts:
            return posts

        # Approach 4: Load from cached JSON snapshot
        posts = self._try_cached_fallback(count)
        if posts:
            return posts

        # All approaches failed
        return {
            "error": (
                "Unable to fetch Truth Social posts for @%s. "
                "This typically happens from cloud/VPN IPs that Truth Social blocks. "
                "If running locally on a residential connection, the data should load. "
                "Otherwise, visit https://truthsocial.com/@%s directly."
                % (username, username)
            )
        }

    # ------------------------------------------------------------------
    def _try_cached_fallback(self, count: int = 20) -> list[dict] | None:
        """Load Truth Social posts from a local JSON cache file."""
        cache_file = _CACHE_DIR / "truthsocial.json"
        try:
            if cache_file.exists():
                with open(cache_file) as f:
                    data = json.load(f)
                if isinstance(data, list) and data:
                    logger.info("Loaded Truth Social data from cached fallback (%d posts)", len(data))
                    return data[:count]
        except Exception as exc:
            logger.info("Failed to load Truth Social cache: %s", exc)
        return None

    # ------------------------------------------------------------------
    def _try_mastodon_api(self, username: str, count: int) -> list[dict] | None:
        """Fetch posts via Truth Social's Mastodon-compatible API."""
        try:
            # Step 1: Get account ID (use known ID if available)
            account_id = self.KNOWN_ACCOUNTS.get(username)
            if not account_id:
                lookup_url = self.API_BASE + self.ACCOUNT_LOOKUP
                resp = self.session.get(
                    lookup_url,
                    params={"acct": username},
                    headers=self.api_headers,
                    timeout=15,
                )
                if resp.status_code != 200:
                    logger.info("Truth Social lookup returned %s", resp.status_code)
                    return None
                account_data = resp.json()
                account_id = account_data.get("id")
                if not account_id:
                    return None

            # Step 2: Fetch statuses
            statuses_url = self.API_BASE + self.ACCOUNT_STATUSES.format(account_id=account_id)
            resp = self.session.get(
                statuses_url,
                params={"limit": count, "exclude_replies": "true"},
                headers=self.api_headers,
                timeout=15,
            )
            if resp.status_code != 200:
                logger.info("Truth Social statuses returned %s", resp.status_code)
                return None

            statuses = resp.json()
            if not isinstance(statuses, list):
                return None

            posts: list[dict] = []
            for status in statuses[:count]:
                # Strip HTML from content
                raw_content = status.get("content", "")
                text = BeautifulSoup(raw_content, "html.parser").get_text(separator=" ", strip=True)

                # Handle reboosts (reblogs)
                reblog = status.get("reblog")
                if reblog and not text:
                    raw_content = reblog.get("content", "")
                    text = BeautifulSoup(raw_content, "html.parser").get_text(separator=" ", strip=True)
                    text = "[Reblog] " + text

                # Get media info
                media = status.get("media_attachments", [])
                has_media = len(media) > 0
                if not text and has_media:
                    text = "[Image/Media post]"

                posts.append({
                    "id": status.get("id", ""),
                    "text": text,
                    "created_at": status.get("created_at", ""),
                    "url": status.get("url", ""),
                    "reblogs_count": status.get("reblogs_count", 0),
                    "favourites_count": status.get("favourites_count", 0),
                    "replies_count": status.get("replies_count", 0),
                    "has_media": has_media,
                })
            return posts if posts else None

        except Exception as exc:
            logger.info("Mastodon API fetch failed for @%s: %s", username, exc)
            return None

    # ------------------------------------------------------------------
    def _try_rss(self, username: str, count: int) -> list[dict] | None:
        url = self.RSS_URL_TEMPLATE.format(username=username)
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                logger.info("RSS feed returned %s for @%s", resp.status_code, username)
                return None
            feed = feedparser.parse(resp.text)
            if not feed.entries:
                return None
            posts: list[dict] = []
            for entry in feed.entries[:count]:
                created_at = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    created_at = datetime(*entry.published_parsed[:6]).isoformat()
                elif hasattr(entry, "published"):
                    try:
                        created_at = dateutil_parser.parse(entry.published).isoformat()
                    except Exception:
                        created_at = entry.published

                raw_text = entry.get("summary", entry.get("title", ""))
                text = BeautifulSoup(raw_text, "html.parser").get_text(separator=" ", strip=True)

                posts.append({
                    "id": entry.get("id", ""),
                    "text": text,
                    "created_at": created_at,
                    "url": entry.get("link", ""),
                })
            return posts
        except Exception as exc:
            logger.info("RSS fetch failed for @%s: %s", username, exc)
            return None

    # ------------------------------------------------------------------
    def _try_playwright(self, username: str, count: int) -> list[dict] | None:
        """Try using Playwright to scrape the Truth Social profile page."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.info("Playwright not installed -- skipping browser-based Truth Social fetch. "
                        "Install with: pip install playwright && playwright install chromium")
            return None

        try:
            url = self.PROFILE_URL_TEMPLATE.format(username=username)
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(user_agent=_HEADERS["User-Agent"])
                page = context.new_page()
                page.goto(url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(3000)

                # Try to get data from the Mastodon API in the browser context
                api_data = page.evaluate("""async () => {
                    try {
                        const lookup = await fetch('/api/v1/accounts/lookup?acct=""" + username + """');
                        if (!lookup.ok) return null;
                        const account = await lookup.json();
                        const statuses = await fetch('/api/v1/accounts/' + account.id + '/statuses?limit=20&exclude_replies=true');
                        if (!statuses.ok) return null;
                        return await statuses.json();
                    } catch(e) { return null; }
                }""")

                browser.close()

                if api_data and isinstance(api_data, list):
                    posts = []
                    for s in api_data[:count]:
                        text = BeautifulSoup(s.get("content", ""), "html.parser").get_text(separator=" ", strip=True)
                        if not text and s.get("media_attachments"):
                            text = "[Image/Media post]"
                        posts.append({
                            "id": s.get("id", ""),
                            "text": text,
                            "created_at": s.get("created_at", ""),
                            "url": s.get("url", ""),
                            "reblogs_count": s.get("reblogs_count", 0),
                            "favourites_count": s.get("favourites_count", 0),
                            "replies_count": s.get("replies_count", 0),
                        })
                    return posts if posts else None
                return None
        except Exception as exc:
            logger.info("Playwright Truth Social scrape failed: %s", exc)
            return None

class DataCache:
    """Thread-safe TTL cache wrapping all data fetchers."""

    def __init__(self):
        self._lock = threading.Lock()
        # Separate TTL caches per data domain
        self._macro_cache: TTLCache = TTLCache(maxsize=1, ttl=config.CACHE_TTL["macro"])
        self._fedwatch_cache: TTLCache = TTLCache(maxsize=1, ttl=config.CACHE_TTL["fedwatch"])
        self._truth_cache: TTLCache = TTLCache(maxsize=1, ttl=config.CACHE_TTL["truthsocial"])

        self.fred = FREDFetcher()
        self.fedwatch = FedWatchFetcher()
        self.truth = TruthSocialFetcher()

        # Track last successful fetch times and errors
        self.status: dict[str, Any] = {
            "macro": {"last_fetch": None, "error": None},
            "fedwatch": {"last_fetch": None, "error": None},
            "truthsocial": {"last_fetch": None, "error": None},
        }

    # ------------------------------------------------------------------
    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Macro indicators
    # ------------------------------------------------------------------
    def get_macro(self, force: bool = False) -> dict:
        with self._lock:
            if not force and "data" in self._macro_cache:
                return self._macro_cache["data"]
        return self.refresh_macro()

    def refresh_macro(self) -> dict:
        try:
            data = self.fred.fetch_all_macro()
            with self._lock:
                self._macro_cache["data"] = data
                self.status["macro"] = {"last_fetch": self._now_iso(), "error": None}
            return data
        except Exception as exc:
            logger.exception("Failed to refresh macro data")
            with self._lock:
                self.status["macro"]["error"] = str(exc)
            return {"_error": str(exc)}

    # ------------------------------------------------------------------
    # FedWatch
    # ------------------------------------------------------------------
    def get_fedwatch(self, force: bool = False) -> dict:
        with self._lock:
            if not force and "data" in self._fedwatch_cache:
                return self._fedwatch_cache["data"]
        return self.refresh_fedwatch()

    def refresh_fedwatch(self) -> dict:
        try:
            data = self.fedwatch.fetch_rate_probabilities()
            with self._lock:
                self._fedwatch_cache["data"] = data
                self.status["fedwatch"] = {"last_fetch": self._now_iso(), "error": None}
            return data
        except Exception as exc:
            logger.exception("Failed to refresh FedWatch data")
            with self._lock:
                self.status["fedwatch"]["error"] = str(exc)
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Truth Social
    # ------------------------------------------------------------------
    def get_truthsocial(self, force: bool = False) -> dict | list:
        with self._lock:
            if not force and "data" in self._truth_cache:
                return self._truth_cache["data"]
        return self.refresh_truthsocial()

    def refresh_truthsocial(self) -> dict | list:
        try:
            data = self.truth.fetch_latest_posts()
            with self._lock:
                self._truth_cache["data"] = data
                self.status["truthsocial"] = {"last_fetch": self._now_iso(), "error": None}
            return data
        except Exception as exc:
            logger.exception("Failed to refresh Truth Social data")
            with self._lock:
                self.status["truthsocial"]["error"] = str(exc)
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    def get_status(self) -> dict:
        with self._lock:
            return {
                "fred_configured": self.fred.is_configured,
                "sources": dict(self.status),
            }
