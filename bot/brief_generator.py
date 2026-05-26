"""
brief_generator.py — orchestrates article fetching, LLM brief generation,
and dispatch to sender.py.

Entry point: run this script directly from GitHub Actions.
  python bot/brief_generator.py                    # all active users
  python bot/brief_generator.py --user 123456789   # single user (manual_brief)
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from itertools import groupby
from typing import Optional

import requests

from news_fetcher import fetch_all_articles, filter_unseen
from sender import send_brief
from supabase_client import (
    cache_articles,
    get_active_users,
    get_cached_articles,
    get_seen_urls,
    get_user,
    log_delivery,
    mark_articles_seen,
    prune_old_articles,
    prune_old_seen_articles,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenRouter config
# ---------------------------------------------------------------------------

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "anthropic/claude-sonnet-4-6"  # routed through OpenRouter

# The system prompt written by Lisa — passed verbatim to the LLM
SYSTEM_PROMPT = """You are an expert Cyber Policy Advisor and Principal Strategic Technical Intelligence Analyst for the Singapore Government. Your role is to deliver a high-visibility executive intelligence brief that synthesizes high-level global and regional cyber developments, emerging infrastructure vulnerabilities, software supply chain threats, and AI governance shifts across public sectors and industry. Your target audience consists of senior public sector leaders and tech policy portfolio managers.

STRICT RULES:
1. ONLY use articles provided to you in the user message. Do NOT invent, hallucinate, or reference any source not explicitly given to you.
2. Every story must include a real, working URL from the provided articles. If no URL is provided for a story, do not include that story.
3. If fewer than 5 real articles are provided, produce a brief with only as many entries as there are verified articles. Do not pad with invented content.
4. Temporal window: only reference articles published within the last 48 hours.
5. Maintain a warm, grounded, highly professional peer voice. No corporate buzzwords.

OUTPUT FORMAT — strict 5-part hierarchy per story:

[Index Number].
[Macro Focus Area]: [High-Impact Headline]

Primary Source: [Source name] ([Date: Month DD, YYYY])
Verified Source Link: [exact URL from provided articles]

The Technical Event: [2-3 sentences, plain English]
The Technology: [1-2 sentences defining the technical concept]
The Policy Impact: [2-3 sentences on governance/regulatory/strategic implications]
Strategic Question for Your Team: "[Actionable diagnostic question in italics]"

End every brief with a Strategic Action Matrix — a 5-row markdown table with columns: Priority Focus | Breaking Threat Flashpoint | Key Regulatory & Policy Framework | Source Link"""

REQUEST_TIMEOUT = 60  # OpenRouter can be slow under load


# ---------------------------------------------------------------------------
# Article formatting for the LLM prompt
# ---------------------------------------------------------------------------

def _format_articles_for_prompt(articles: list[dict]) -> str:
    """Render article list as numbered text for the LLM user message."""
    lines = []
    for i, a in enumerate(articles, 1):
        pub = a["published_at"]
        date_str = pub.strftime("%B %d, %Y") if pub else "Unknown date"
        lines.append(
            f"{i}. Title: {a['title']}\n"
            f"   Source: {a['source']}\n"
            f"   Published: {date_str}\n"
            f"   URL: {a['url']}\n"
            f"   Summary: {a.get('summary', '')}\n"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# OpenRouter call
# ---------------------------------------------------------------------------

def call_openrouter(
    articles: list[dict],
    api_key: str,
    preferences: Optional[str] = None,
) -> Optional[str]:
    """
    Send articles to OpenRouter and return the generated brief text.
    Returns None on failure so the caller can log and continue.
    """
    user_message = (
        "Please generate the cyber intelligence brief based on the following "
        "articles. Use ONLY these articles — do not reference any other sources.\n\n"
        + _format_articles_for_prompt(articles)
    )
    if preferences:
        user_message += f"\n\nBrief customisation for this recipient: {preferences}"

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": 4096,
        "temperature": 0.3,  # low temperature = consistent, factual output
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # OpenRouter asks for these headers for attribution/analytics
        "HTTP-Referer": "https://github.com/lisassidequests/news-brief",
        "X-Title": "CyberBriefBot",
    }

    try:
        resp = requests.post(
            OPENROUTER_ENDPOINT,
            json=payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.error("OpenRouter call failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Format variants
# ---------------------------------------------------------------------------

def apply_format(full_brief: str, fmt: str) -> str:
    """
    Transform the full brief into the user's preferred format.

    'full'  — return as-is
    'tldr'  — keep only the headline + Policy Impact per story, plus Action Matrix
    'links' — keep only headline + URL per story, plus Action Matrix
    """
    if fmt == "full":
        return full_brief

    lines = full_brief.split("\n")
    output_lines = []

    if fmt == "tldr":
        # Keep lines that are a story header (start with a digit + '.'),
        # Policy Impact lines, and the Strategic Action Matrix block.
        in_matrix = False
        capture_policy = False
        for line in lines:
            stripped = line.strip()
            if "Strategic Action Matrix" in stripped:
                in_matrix = True
            if in_matrix:
                output_lines.append(line)
                continue
            # Story index line e.g. "1."
            if stripped and stripped[0].isdigit() and stripped.endswith("."):
                output_lines.append(line)
                capture_policy = False
                continue
            # Macro focus headline line (contains ": ")
            if "]: [" in stripped or (stripped and stripped[0] == "["):
                output_lines.append(line)
                continue
            if stripped.startswith("The Policy Impact:"):
                capture_policy = True
            if capture_policy:
                output_lines.append(line)
                # Stop capturing after the policy block ends (empty line follows)
                if stripped == "" and capture_policy:
                    capture_policy = False

    elif fmt == "links":
        in_matrix = False
        for line in lines:
            stripped = line.strip()
            if "Strategic Action Matrix" in stripped:
                in_matrix = True
            if in_matrix:
                output_lines.append(line)
                continue
            # Keep story headline lines and verified URL lines only
            if stripped and stripped[0].isdigit() and stripped.endswith("."):
                output_lines.append(line)
            elif "]: [" in stripped or (stripped and stripped[0] == "[" and "Headline" not in stripped):
                output_lines.append(line)
            elif stripped.startswith("Verified Source Link:"):
                output_lines.append(line)
                output_lines.append("")  # blank line separator

    return "\n".join(output_lines)


# ---------------------------------------------------------------------------
# Topic grouping — share one LLM call for users with identical topic sets
# ---------------------------------------------------------------------------

def _topic_key(user: dict) -> str:
    """Stable string key for a user's topic list (sorted for consistency)."""
    topics = user.get("topics") or []
    if isinstance(topics, str):
        topics = json.loads(topics)
    return json.dumps(sorted(topics))


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def process_user(
    user: dict,
    brief_text: str,
    article_urls: list[str],
) -> None:
    """Send the brief to one user, then record state in Supabase."""
    telegram_id = user["telegram_id"]
    fmt = user.get("format", "full")
    formatted_brief = apply_format(brief_text, fmt)

    try:
        await send_brief(
            telegram_id=telegram_id,
            brief_text=formatted_brief,
            bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        )
        mark_articles_seen(telegram_id, article_urls)
        log_delivery(
            user_id=telegram_id,
            status="success",
            article_count=len(article_urls),
        )
        logger.info("Brief sent to user %d (%d articles)", telegram_id, len(article_urls))
    except Exception as exc:
        error_msg = str(exc)
        log_delivery(
            user_id=telegram_id,
            status="failed",
            article_count=0,
            error_message=error_msg,
        )
        logger.error("Failed to send brief to user %d: %s", telegram_id, error_msg)


async def run(
    target_telegram_id: Optional[int] = None,
    force_refresh: bool = False,
) -> None:
    """
    Full pipeline:
      1. Prune stale seen_articles and cached articles
      2. Load articles from Supabase cache, or fetch fresh from NewsAPI + RSS
      3. Group active users by topic set
      4. For each group, call OpenRouter once, send to all group members

    Set force_refresh=True to bypass the article cache and always re-fetch.
    """
    prune_old_seen_articles()
    prune_old_articles()

    newsapi_key = os.environ["NEWSAPI_KEY"]
    openrouter_key = os.environ["OPENROUTER_API_KEY"]

    # Try the article cache first (skips NewsAPI + RSS calls when fresh)
    all_articles: list[dict] = []
    if not force_refresh:
        all_articles = get_cached_articles()

    if all_articles:
        logger.info("Using %d cached articles (skipping NewsAPI/RSS fetch)", len(all_articles))
    else:
        if force_refresh:
            logger.info("force_refresh=True — fetching fresh articles")
        else:
            logger.info("Article cache miss — fetching from NewsAPI and RSS feeds")
        all_articles = fetch_all_articles(newsapi_key)
        if all_articles:
            cache_articles(all_articles)  # persist for the next run

    if not all_articles:
        logger.warning("No articles available — aborting brief run")
        return

    # Determine which users to process
    if target_telegram_id:
        user = get_user(target_telegram_id)
        if not user:
            logger.error("User %d not found in database", target_telegram_id)
            return
        users = [user]
    else:
        users = get_active_users()

    if not users:
        logger.info("No active users to send briefs to")
        return

    logger.info("Processing %d user(s)", len(users))

    # Group by topic key to reuse LLM calls
    users_sorted = sorted(users, key=_topic_key)
    tasks = []

    for topic_key, group_iter in groupby(users_sorted, key=_topic_key):
        group = list(group_iter)
        topics = json.loads(topic_key)

        # Filter articles to those relevant to this group's topics
        # If the user has no topics specified, use all articles
        if topics:
            relevant = [
                a for a in all_articles
                if any(
                    t.lower() in (a["title"] + " " + a.get("summary", "")).lower()
                    for t in topics
                )
            ]
            # Fall back to all articles if topic filter leaves nothing
            if not relevant:
                relevant = all_articles
        else:
            relevant = all_articles

        # Cap at 20 articles per LLM call to keep prompt size manageable
        relevant = relevant[:20]

        logger.info(
            "Topic group %r: %d users, %d articles → calling OpenRouter",
            topic_key,
            len(group),
            len(relevant),
        )

        article_urls = [a["url"] for a in relevant]

        # Partition group: users with preferences need individual LLM calls
        users_no_prefs   = [u for u in group if not u.get("preferences")]
        users_with_prefs = [u for u in group if u.get("preferences")]

        # Shared brief for users with no preferences (preserves grouping optimisation)
        if users_no_prefs:
            brief_text = call_openrouter(relevant, openrouter_key)
            if brief_text:
                for user in users_no_prefs:
                    seen = get_seen_urls(user["telegram_id"])
                    tasks.append(process_user(user, brief_text,
                                              [u for u in article_urls if u not in seen]))
            else:
                for user in users_no_prefs:
                    log_delivery(user_id=user["telegram_id"], status="failed",
                                 error_message="OpenRouter returned no content")

        # Per-user brief for users with custom preferences (capped at 200 chars)
        for user in users_with_prefs:
            prefs = (user.get("preferences") or "")[:200]
            brief_text = call_openrouter(relevant, openrouter_key, prefs)
            if brief_text:
                seen = get_seen_urls(user["telegram_id"])
                tasks.append(process_user(user, brief_text,
                                          [u for u in article_urls if u not in seen]))
            else:
                log_delivery(user_id=user["telegram_id"], status="failed",
                             error_message="OpenRouter returned no content")

    # Run all sends concurrently (sender.py handles per-user rate limiting)
    await asyncio.gather(*tasks)
    logger.info("Brief run complete")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cyber Intel Brief Generator")
    parser.add_argument(
        "--user",
        type=int,
        default=None,
        help="Telegram user ID to send brief to (omit for all active users)",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        default=False,
        help="Bypass the Supabase article cache and re-fetch from NewsAPI/RSS",
    )
    args = parser.parse_args()

    asyncio.run(run(target_telegram_id=args.user, force_refresh=args.force_refresh))
