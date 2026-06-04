"""
news_fetcher.py — fetches cybersecurity articles from authoritative RSS feeds.

All sources are hand-curated for authority and signal quality:
  - Government primary sources (CISA, Singapore CSA, ENISA)
  - Tier-1 threat intelligence (Mandiant, Recorded Future, Talos, Unit 42)
  - Authoritative security journalism (Krebs, SecurityWeek, THN, Bleeping Computer, The Record)

Results are deduplicated by URL, filtered to the last 48 hours, and returned
as a list of article dicts that the brief generator can consume.
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

RSS_FEEDS = {
    # Government primary sources
    "CISA":           "https://www.cisa.gov/cybersecurity-advisories/all.xml",
    "Singapore CSA":  "https://www.csa.gov.sg/alerts-and-advisories/",
    "ENISA":          "https://www.enisa.europa.eu/topics/enisa-news/rss-feed",
    # Tier-1 threat intelligence
    "Mandiant":       "https://www.mandiant.com/resources/blog/rss.xml",
    "Recorded Future":"https://www.recordedfuture.com/feed",
    "Cisco Talos":    "https://blog.talosintelligence.com/feeds/posts/default",
    "Unit 42":        "https://unit42.paloaltonetworks.com/feed/",
    # Authoritative security journalism
    "KrebsOnSecurity":"https://krebsonsecurity.com/feed/",
    "SecurityWeek":   "https://www.securityweek.com/feed/",
    "The Hacker News":"https://feeds.feedburner.com/TheHackersNews",
    "Bleeping Computer": "https://www.bleepingcomputer.com/feed/",
    "The Record":     "https://therecord.media/feed",
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

def fetch_rss_articles() -> list[dict]:
    """
    Fetch articles from all authoritative RSS feeds.
    Returns a list of article dicts (not yet deduplicated).
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
    """Remove duplicate URLs, keeping first occurrence."""
    seen_urls: set[str] = set()
    unique = []
    for article in articles:
        url = article["url"]
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique.append(article)
    return unique


def fetch_all_articles() -> list[dict]:
    """
    Main entry point. Fetches from all RSS feeds, deduplicates by URL.
    Does NOT filter against per-user seen_articles — that happens in
    brief_generator.py after we know which user we're generating for.
    """
    rss_articles = fetch_rss_articles()
    unique = deduplicate(rss_articles)
    logger.info("Total unique articles after dedup: %d", len(unique))
    return unique


def filter_unseen(articles: list[dict], seen_urls: set[str]) -> list[dict]:
    """Return only articles whose URL is not in `seen_urls`."""
    return [a for a in articles if a["url"] not in seen_urls]
