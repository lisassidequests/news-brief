"""
news_fetcher.py — fetches cybersecurity articles from NewsAPI and RSS feeds.

Two sources are combined:
  1. NewsAPI  — keyword search over the last 48 hours (up to 20 articles)
  2. RSS feeds — CISA, KrebsOnSecurity, Mandiant (always included)

Results are deduplicated by URL, filtered against each user's seen_articles,
and returned as a list of article dicts that the brief generator can consume.
"""

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional
import xml.etree.ElementTree as ET

import requests
import feedparser  # pure-Python RSS/Atom parser, no compiled binary

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NEWSAPI_ENDPOINT = "https://newsapi.org/v2/everything"
NEWSAPI_KEYWORDS = (
    'cybersecurity OR "cyber attack" OR "AI governance" OR "supply chain" '
    'OR CISA OR "threat intelligence"'
)
NEWSAPI_MAX_RESULTS = 20

RSS_FEEDS = {
    "CISA": "https://www.cisa.gov/cybersecurity-advisories/all.xml",
    "KrebsOnSecurity": "https://krebsonsecurity.com/feed/",
    "Mandiant": "https://www.mandiant.com/resources/blog/rss.xml",
}

# Articles older than this are ignored
MAX_AGE_HOURS = 48

REQUEST_TIMEOUT = 15  # seconds


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_recent(dt: Optional[datetime]) -> bool:
    """Return True if `dt` is within the last 48 hours (or if unknown)."""
    if dt is None:
        return True  # keep articles with no date rather than silently drop them
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    # Make dt timezone-aware if it isn't already
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= cutoff


def _parse_feedparser_entry(entry, source_name: str) -> dict:
    """Convert a feedparser entry into our standard article dict."""
    # feedparser exposes published_parsed as a time.struct_time in UTC
    published_at = None
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

    # Best-effort summary: use 'summary' field, strip any HTML tags crudely
    summary = getattr(entry, "summary", "") or ""
    summary = summary[:500]  # keep it short for prompt efficiency

    return {
        "title": getattr(entry, "title", "No title"),
        "url": getattr(entry, "link", ""),
        "source": source_name,
        "published_at": published_at,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_newsapi_articles(api_key: str) -> list[dict]:
    """
    Query NewsAPI for recent cybersecurity articles.
    Returns a list of article dicts; empty list on error.
    """
    from_date = (datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    params = {
        "q": NEWSAPI_KEYWORDS,
        "from": from_date,
        "sortBy": "publishedAt",
        "pageSize": NEWSAPI_MAX_RESULTS,
        "language": "en",
        "apiKey": api_key,
    }
    try:
        resp = requests.get(NEWSAPI_ENDPOINT, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("NewsAPI fetch failed: %s", exc)
        return []

    articles = []
    for item in data.get("articles", []):
        url = item.get("url", "")
        if not url:
            continue

        published_at = None
        raw_date = item.get("publishedAt")
        if raw_date:
            try:
                published_at = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            except ValueError:
                pass

        if not _is_recent(published_at):
            continue

        articles.append(
            {
                "title": item.get("title") or "No title",
                "url": url,
                "source": item.get("source", {}).get("name") or "NewsAPI",
                "published_at": published_at,
                "summary": (item.get("description") or "")[:500],
            }
        )

    logger.info("NewsAPI returned %d recent articles", len(articles))
    return articles


def fetch_rss_articles() -> list[dict]:
    """
    Fetch articles from the three fixed RSS feeds.
    Always included regardless of NewsAPI results.
    Returns a deduplicated list of article dicts.
    """
    articles = []
    for source_name, feed_url in RSS_FEEDS.items():
        try:
            # feedparser handles HTTP itself; we pass agent to be polite
            feed = feedparser.parse(
                feed_url,
                request_headers={"User-Agent": "CyberBriefBot/1.0"},
            )
            if feed.bozo and not feed.entries:
                # bozo=True means the feed had parse errors; skip if no entries recovered
                logger.warning("RSS feed %s had parse errors and no entries", source_name)
                continue

            for entry in feed.entries:
                article = _parse_feedparser_entry(entry, source_name)
                if article["url"] and _is_recent(article["published_at"]):
                    articles.append(article)

            logger.info("RSS %s: %d entries fetched", source_name, len(feed.entries))
        except Exception as exc:
            logger.error("RSS feed %s failed: %s", source_name, exc)
            continue

    return articles


def deduplicate(articles: list[dict]) -> list[dict]:
    """Remove duplicate URLs, keeping first occurrence (NewsAPI priority)."""
    seen_urls: set[str] = set()
    unique = []
    for article in articles:
        url = article["url"]
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique.append(article)
    return unique


def fetch_all_articles(api_key: str) -> list[dict]:
    """
    Main entry point.  Merges NewsAPI + RSS, deduplicates by URL.
    Does NOT filter against per-user seen_articles — that happens in
    brief_generator.py after we know which user we're generating for.
    """
    newsapi_articles = fetch_newsapi_articles(api_key)
    rss_articles = fetch_rss_articles()

    # RSS feeds come after NewsAPI so NewsAPI wins on URL conflicts
    combined = newsapi_articles + rss_articles
    unique = deduplicate(combined)

    logger.info(
        "Total unique articles after merge: %d (NewsAPI=%d, RSS=%d)",
        len(unique),
        len(newsapi_articles),
        len(rss_articles),
    )
    return unique


def filter_unseen(articles: list[dict], seen_urls: set[str]) -> list[dict]:
    """Return only articles whose URL is not in `seen_urls`."""
    return [a for a in articles if a["url"] not in seen_urls]
