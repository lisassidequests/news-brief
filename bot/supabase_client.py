"""
supabase_client.py — thin wrapper around the Supabase REST API.

All database access for the bot goes through this module.  We use the
`supabase-py` library (which itself wraps PostgREST) rather than raw HTTP so
that query building stays readable.  The service-role key bypasses RLS and
lets the bot read/write any row.
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from supabase import create_client, Client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Client singleton — created once at import time.
# ---------------------------------------------------------------------------

def _build_client() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return create_client(url, key)


_client: Optional[Client] = None


def get_client() -> Client:
    """Return (or lazily initialise) the shared Supabase client."""
    global _client
    if _client is None:
        _client = _build_client()
    return _client


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

def get_active_users() -> list[dict]:
    """Return all rows from `users` where is_active = true."""
    resp = get_client().table("users").select("*").eq("is_active", True).execute()
    return resp.data or []


def get_user(telegram_id: int) -> Optional[dict]:
    """Fetch a single user by Telegram ID, or None if not found."""
    resp = (
        get_client()
        .table("users")
        .select("*")
        .eq("telegram_id", telegram_id)
        .limit(1)
        .execute()
    )
    return resp.data[0] if resp.data else None


def upsert_user(data: dict) -> dict:
    """Insert or update a user row.  `data` must include `telegram_id`."""
    resp = (
        get_client()
        .table("users")
        .upsert(data, on_conflict="telegram_id")
        .execute()
    )
    return resp.data[0] if resp.data else {}


def set_user_active(telegram_id: int, active: bool) -> None:
    """Toggle is_active for a user (used by /pause and /resume)."""
    get_client().table("users").update({"is_active": active}).eq(
        "telegram_id", telegram_id
    ).execute()


def get_all_users() -> list[dict]:
    """Return every user row (admin /users command)."""
    resp = get_client().table("users").select("*").execute()
    return resp.data or []


# ---------------------------------------------------------------------------
# Seen-articles helpers
# ---------------------------------------------------------------------------

def prune_old_seen_articles() -> int:
    """Delete seen_articles rows older than 7 days.  Returns number deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    resp = (
        get_client()
        .table("seen_articles")
        .delete()
        .lt("seen_at", cutoff)
        .execute()
    )
    deleted = len(resp.data) if resp.data else 0
    logger.info("Pruned %d old seen_articles rows", deleted)
    return deleted


def get_seen_urls(user_id: int) -> set[str]:
    """Return the set of article URLs already seen by this user."""
    resp = (
        get_client()
        .table("seen_articles")
        .select("article_url")
        .eq("user_id", user_id)
        .execute()
    )
    return {row["article_url"] for row in (resp.data or [])}


def mark_articles_seen(user_id: int, urls: list[str]) -> None:
    """Insert rows for each URL so the user won't receive them again."""
    if not urls:
        return
    rows = [{"user_id": user_id, "article_url": url} for url in urls]
    # ignore_duplicates=True handles the UNIQUE constraint gracefully
    get_client().table("seen_articles").upsert(
        rows, on_conflict="user_id,article_url"
    ).execute()


# ---------------------------------------------------------------------------
# Delivery-log helpers
# ---------------------------------------------------------------------------

def log_delivery(
    user_id: int,
    status: str,
    article_count: int = 0,
    error_message: Optional[str] = None,
) -> None:
    """Write one row to delivery_log after a send attempt."""
    get_client().table("delivery_log").insert(
        {
            "user_id": user_id,
            "status": status,
            "article_count": article_count,
            "error_message": error_message,
        }
    ).execute()


def log_feedback(user_id: int, rating: str) -> None:
    """Insert one row into brief_feedback (rating = 'up' or 'down')."""
    get_client().table("brief_feedback").insert(
        {"user_id": user_id, "rating": rating}
    ).execute()


def get_recent_logs(days: int = 7) -> list[dict]:
    """Fetch delivery_log rows from the last N days (admin /logs command)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    resp = (
        get_client()
        .table("delivery_log")
        .select("*")
        .gte("sent_at", cutoff)
        .order("sent_at", desc=True)
        .execute()
    )
    return resp.data or []


# ---------------------------------------------------------------------------
# Article cache helpers
# ---------------------------------------------------------------------------

def get_cached_articles(max_age_hours: int = 24) -> list[dict]:
    """
    Return articles fetched within the last `max_age_hours` hours.

    If the result is non-empty the cache is considered fresh and the caller
    should skip re-fetching from NewsAPI/RSS.  Returns an empty list when the
    cache is stale or unpopulated (cache miss → caller must fetch fresh).

    Converts `published_at` from the ISO string stored in Postgres back to a
    UTC-aware datetime so the returned dicts match the shape produced by
    news_fetcher.fetch_all_articles().
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    resp = (
        get_client()
        .table("articles")
        .select("url,title,source,published_at,summary")
        .gte("fetched_at", cutoff)
        .execute()
    )
    rows = resp.data or []
    articles = []
    for row in rows:
        pub = None
        if row.get("published_at"):
            try:
                pub = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        articles.append(
            {
                "title": row["title"],
                "url": row["url"],
                "source": row["source"],
                "published_at": pub,
                "summary": row.get("summary") or "",
            }
        )
    logger.info("Article cache returned %d articles (max_age=%dh)", len(articles), max_age_hours)
    return articles


def cache_articles(articles: list[dict]) -> None:
    """
    Upsert a list of article dicts to the `articles` table.

    On URL conflict, `fetched_at` is updated to NOW() so the freshness clock
    resets — "last fetched wins".  `published_at` is serialised from a Python
    datetime to an ISO string for Postgres TIMESTAMPTZ storage.
    """
    if not articles:
        return
    rows = []
    for a in articles:
        pub = a.get("published_at")
        rows.append(
            {
                "url": a["url"],
                "title": a["title"],
                "source": a["source"],
                "published_at": pub.isoformat() if pub else None,
                "summary": a.get("summary") or "",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    get_client().table("articles").upsert(rows, on_conflict="url").execute()
    logger.info("Cached %d articles to Supabase", len(rows))


def prune_old_articles(max_age_hours: int = 48) -> int:
    """
    Delete articles rows older than `max_age_hours`.

    Called at the start of each brief run alongside prune_old_seen_articles()
    to keep the table small.  Returns the number of rows deleted.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    resp = (
        get_client()
        .table("articles")
        .delete()
        .lt("fetched_at", cutoff)
        .execute()
    )
    deleted = len(resp.data) if resp.data else 0
    logger.info("Pruned %d old articles rows", deleted)
    return deleted


# ---------------------------------------------------------------------------
# Brief cache helpers
# ---------------------------------------------------------------------------

def get_cached_brief(user_id: int, format: str) -> Optional[str]:
    """Return cached brief text for (user_id, format), or None if not cached."""
    try:
        resp = (
            get_client()
            .table("brief_cache")
            .select("brief_text")
            .eq("user_id", user_id)
            .eq("format", format)
            .limit(1)
            .execute()
        )
        return resp.data[0]["brief_text"] if resp.data else None
    except Exception as exc:
        logger.warning("brief_cache read failed: %s", exc)
        return None


def set_cached_brief(user_id: int, format: str, brief_text: str) -> None:
    """Upsert the cached brief for (user_id, format)."""
    get_client().table("brief_cache").upsert(
        {"user_id": user_id, "format": format, "brief_text": brief_text},
        on_conflict="user_id,format",
    ).execute()
