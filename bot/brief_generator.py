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
from datetime import datetime, timezone, timedelta
from itertools import groupby
from typing import Optional

import requests

from news_fetcher import fetch_all_articles, filter_unseen
from sender import send_brief
from supabase_client import (
    cache_articles,
    get_active_users,
    get_cached_articles,
    get_cached_brief,
    get_seen_urls,
    get_user,
    log_delivery,
    mark_articles_seen,
    prune_old_articles,
    prune_old_seen_articles,
    set_cached_brief,
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

# The system prompt — passed verbatim to the LLM
SYSTEM_PROMPT = """You are an expert Cyber Policy Advisor and Principal Strategic Technical Intelligence Analyst for the Singapore Government. Your role is to deliver a high-visibility executive intelligence brief that synthesizes global and regional cyber developments, infrastructure vulnerabilities, supply chain threats, and AI governance shifts. Your target audience consists of senior public sector leaders and tech policy portfolio managers.

STRICT RULES:
1. ONLY use articles provided to you in the user message. Do NOT invent, hallucinate, or reference any source not explicitly given to you.
2. Every story must include a real, working URL from the provided articles. If no URL is provided for a story, do not include that story.
3. If fewer than 5 real articles are provided, produce a brief with only as many entries as there are verified articles. Do not pad with invented content.
4. Temporal window: only reference articles published within the last 24 hours (previous day). If fewer articles are available, include only those that exist — do not pad with older content.
5. Maintain a warm, grounded, highly professional peer voice. No corporate buzzwords.
6. Where stories have Singapore or APAC relevance, name the specific agency, legislation, or framework: CSA, IMDA, MAS, CII operators, Cybersecurity Act, Cybersecurity (Amendment) Act 2024, PDPA, ASEAN Digital Masterplan. Do not genericise these references.

OPENING:
Begin with a single narrative paragraph (3–5 sentences) that:
- Opens with today's date and "Here is your curated Cyber & AI Policy Brief"
- Identifies which of the recipient's focus areas today's stories speak to
- Sets the analytical frame for the day

OUTPUT FORMAT — strict structure per story. Use Telegram MarkdownV1 syntax (*bold*, _italic_, [text](url) — NOT **double asterisk**):

*[N]. [Category Label]: [High-Impact Headline]*
_Primary Source: [Publication name] / [Secondary source if relevant] ([Date range, e.g. May 16–17, 2026])_
[Read the full article]([exact URL from provided articles])

*The Technical Event:* [2–3 sentences, factual plain-English description of what happened]

*The Policy Implication* _([Geographic scope — include Singapore focus where relevant]):_
[3–4 sentences covering: what this event means for governance or regulatory portfolios; which specific Singapore agencies, legislation, or frameworks are implicated (CSA, IMDA, MAS, CII operators, Cybersecurity Act, Cybersecurity (Amendment) Act 2024, PDPA, ASEAN Digital Masterplan); and one diagnostic question or action a senior official should put to their team.]

CLOSING:
After all stories, end with a table titled *Strategic Executive Summary for Your Briefing* — one row per story:

| Core Focus | Today's Flashpoint | Immediate Policy Question to Ask Your Team |
|---|---|---|
| [Domain label, e.g. "Critical Infrastructure"] | [Key event in ≤12 words] | [One sharp question a senior official should put to their team] |

Do not include a Source Link column. URLs are already in each story body."""

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
    sgt_today = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%A, %d %B %Y")
    user_message = (
        f"Today's date (Singapore Time): {sgt_today}\n\n"
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
    'tldr'  — opening paragraph + headline + source + Policy Impact per story
    'links' — opening paragraph + headline + article link per story
    """
    import re as _re

    if fmt == "full":
        return full_brief

    lines = full_brief.split("\n")

    # Find where the first numbered story begins (*1. … or 1. …)
    first_story_idx = next(
        (i for i, l in enumerate(lines) if _re.match(r"^\*?\d+\.", l.strip())),
        len(lines),
    )

    # Always keep the opening narrative paragraph
    output_lines = list(lines[:first_story_idx])

    if fmt == "tldr":
        in_policy = False
        for line in lines[first_story_idx:]:
            stripped = line.strip()
            if _re.match(r"^\*?\d+\.", stripped):
                output_lines.append("")
                output_lines.append(line)
                in_policy = False
            elif stripped.startswith("_Primary Source"):
                output_lines.append(line)
            elif stripped.startswith("[Read the full article]"):
                output_lines.append(line)
            elif "Policy Implication" in stripped or "Policy Impact" in stripped:
                in_policy = True
                output_lines.append(line)
            elif in_policy:
                # Next bold section label ends the policy block
                if stripped.startswith("*The "):
                    in_policy = False
                else:
                    output_lines.append(line)

    elif fmt == "links":
        for line in lines[first_story_idx:]:
            stripped = line.strip()
            if _re.match(r"^\*?\d+\.", stripped):
                output_lines.append("")
                output_lines.append(line)
            elif stripped.startswith("[Read the full article]"):
                output_lines.append(line)

    return "\n".join(output_lines).strip()


# ---------------------------------------------------------------------------
# Article prioritization
# ---------------------------------------------------------------------------

_SOURCE_TIER: dict[str, int] = {
    # Government primary sources — cybersecurity
    "CISA":             30,
    "Singapore CSA":    30,
    "ENISA":            30,
    # Singapore & EU AI governance (primary sources)
    "AI Verify Foundation": 30,
    "EU AI Office":     30,
    # Tier-1 threat intelligence
    "Mandiant":         20,
    "Recorded Future":  20,
    "Cisco Talos":      20,
    "Unit 42":          20,
    # AI policy & governance research
    "Stanford HAI":     20,
    "CSET":             20,
    "OECD AI":          20,
    # Authoritative security and financial journalism
    "KrebsOnSecurity":  10,
    "SecurityWeek":     10,
    "The Hacker News":  10,
    "Bleeping Computer": 10,
    "The Record":       10,
    "Bloomberg":        10,
    "Reuters":          10,
    # AI news
    "Financial Times Tech":     10,
    "MIT Technology Review AI": 10,
    "Semafor Technology":       10,
}

# Sources considered US-domestic in scope for the energy cap below
_AMERICAN_SOURCES: frozenset[str] = frozenset({
    "CISA", "KrebsOnSecurity", "SecurityWeek", "The Hacker News",
    "Bleeping Computer", "The Record", "Bloomberg", "Reuters",
})

# Keywords that flag an article as primarily about the energy sector
_ENERGY_KEYWORDS: frozenset[str] = frozenset({
    "energy sector", "power grid", "electric grid", "electricity grid",
    "oil pipeline", "natural gas", "petroleum", "nuclear power plant",
    "power plant", "pipeline attack", "energy infrastructure",
    "utility company", "critical energy", "coal plant", "grid attack",
})


def _score_article(article: dict, topics: list[str]) -> int:
    score = _SOURCE_TIER.get(article["source"], 5)
    text = (article["title"] + " " + article.get("summary", "")).lower()
    for topic in topics:
        if topic.lower() in text:
            score += 5
    pub = article.get("published_at")
    if pub:
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - pub).total_seconds() / 3600
        if age_hours <= 12:
            score += 10
        elif age_hours <= 24:
            score += 5
    return score


def _is_us_energy_article(article: dict) -> bool:
    """Return True if the article is from an American source and covers energy."""
    if article.get("source") not in _AMERICAN_SOURCES:
        return False
    text = (article["title"] + " " + article.get("summary", "")).lower()
    return any(kw in text for kw in _ENERGY_KEYWORDS)


def _prioritize_articles(articles: list[dict], topics: list[str], limit: int) -> list[dict]:
    """
    Return the top `limit` articles by score, with US energy articles capped at 1.

    Scoring runs first, then the energy cap removes excess energy articles from
    American sources before the top-N slice — so the brief fills with other
    relevant articles rather than leaving gaps.
    """
    scored = sorted(articles, key=lambda a: _score_article(a, topics), reverse=True)
    # Apply US energy cap: keep at most 1 energy article from American sources
    us_energy_seen = 0
    capped = []
    for a in scored:
        if _is_us_energy_article(a):
            if us_energy_seen < 1:
                capped.append(a)
                us_energy_seen += 1
        else:
            capped.append(a)
    return capped[:limit]


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
    format_override: Optional[str] = None,
) -> None:
    """Send the brief to one user, then record state in Supabase."""
    telegram_id = user["telegram_id"]
    fmt = format_override or user.get("format", "tldr")
    formatted_brief = apply_format(brief_text, fmt)

    try:
        await send_brief(
            telegram_id=telegram_id,
            brief_text=formatted_brief,
            bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        )
        mark_articles_seen(telegram_id, article_urls)
        set_cached_brief(telegram_id, fmt, formatted_brief)
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
    format_override: Optional[str] = None,
) -> None:
    """
    Full pipeline:
      1. Prune stale seen_articles and cached articles
      2. Load articles from Supabase cache, or fetch fresh from RSS feeds
      3. Group active users by topic set
      4. For each group, call OpenRouter once, send to all group members

    Set force_refresh=True to bypass the article cache and always re-fetch.
    """
    prune_old_seen_articles()
    prune_old_articles()

    openrouter_key = os.environ["OPENROUTER_API_KEY"]

    # Try the article cache first (skips RSS fetches when fresh)
    all_articles: list[dict] = []
    if not force_refresh:
        all_articles = get_cached_articles()

    if all_articles:
        logger.info("Using %d cached articles (skipping RSS fetch)", len(all_articles))
    else:
        if force_refresh:
            logger.info("force_refresh=True — fetching fresh articles")
        else:
            logger.info("Article cache miss — fetching from RSS feeds")
        all_articles = fetch_all_articles()
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
        # Check brief cache for single-user requests (skip LLM if warm)
        if not force_refresh:
            fmt = format_override or user.get("format", "tldr")
            cached_brief = get_cached_brief(target_telegram_id, fmt)
            if cached_brief:
                logger.info("Cache hit for user %d (%s) — sending cached brief", target_telegram_id, fmt)
                try:
                    await send_brief(
                        telegram_id=target_telegram_id,
                        brief_text=cached_brief,
                        bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
                    )
                    log_delivery(user_id=target_telegram_id, status="success", article_count=0)
                except Exception as exc:
                    log_delivery(
                        user_id=target_telegram_id,
                        status="failed",
                        error_message=str(exc),
                    )
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
    article_limit = 5 if format_override == "tldr" else 20

    for topic_key, group_iter in groupby(users_sorted, key=_topic_key):
        group = list(group_iter)
        topics = json.loads(topic_key)

        # Partition group: users with preferences need individual LLM calls
        users_no_prefs   = [u for u in group if not u.get("preferences")]
        users_with_prefs = [u for u in group if u.get("preferences")]

        # Shared brief — fetch each user's seen URLs once, union them, then filter
        # all_articles before passing to the LLM. This prevents yesterday's stories
        # from appearing in today's brief while preserving the single-call optimisation.
        if users_no_prefs:
            seen_per_user = {
                u["telegram_id"]: get_seen_urls(u["telegram_id"])
                for u in users_no_prefs
            }
            group_seen: set[str] = set().union(*seen_per_user.values())
            fresh = [a for a in all_articles if a["url"] not in group_seen]
            relevant = _prioritize_articles(fresh, topics, article_limit)

            logger.info(
                "Topic group %r (no-prefs): %d users, %d fresh articles → calling OpenRouter",
                topic_key, len(users_no_prefs), len(relevant),
            )

            if relevant:
                brief_text = call_openrouter(relevant, openrouter_key)
                if brief_text:
                    article_urls = [a["url"] for a in relevant]
                    for user in users_no_prefs:
                        user_seen = seen_per_user[user["telegram_id"]]
                        new_urls = [url for url in article_urls if url not in user_seen]
                        tasks.append(process_user(user, brief_text, new_urls, format_override))
                else:
                    for user in users_no_prefs:
                        log_delivery(user_id=user["telegram_id"], status="failed",
                                     error_message="OpenRouter returned no content")
            else:
                logger.info("No new articles for group %r — no brief sent today", topic_key)

        # Per-user brief for users with custom preferences (capped at 200 chars)
        for user in users_with_prefs:
            user_seen = get_seen_urls(user["telegram_id"])
            fresh = [a for a in all_articles if a["url"] not in user_seen]
            relevant = _prioritize_articles(fresh, topics, article_limit)

            logger.info(
                "User %d (with-prefs): %d fresh articles → calling OpenRouter",
                user["telegram_id"], len(relevant),
            )

            if not relevant:
                logger.info("No new articles for user %d — no brief sent today", user["telegram_id"])
                continue

            prefs = (user.get("preferences") or "")[:200]
            brief_text = call_openrouter(relevant, openrouter_key, prefs)
            if brief_text:
                tasks.append(process_user(user, brief_text,
                                          [a["url"] for a in relevant]))
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
    import argparse as _argparse

    def _telegram_id(value: str) -> int:
        if value.startswith("@"):
            raise _argparse.ArgumentTypeError(
                f"{value!r} is a username — use the numeric Telegram user ID instead. "
                "Find it via @userinfobot or the /users admin command."
            )
        try:
            return int(value)
        except ValueError:
            raise _argparse.ArgumentTypeError(
                f"{value!r} is not a valid Telegram user ID (must be a plain integer)"
            )

    parser = argparse.ArgumentParser(description="Cyber Intel Brief Generator")
    parser.add_argument(
        "--user",
        type=_telegram_id,
        default=None,
        help="Numeric Telegram user ID (e.g. 254816375). Use /users admin command to look up IDs.",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        default=False,
        help="Bypass the Supabase article cache and re-fetch from RSS feeds",
    )
    parser.add_argument(
        "--format",
        choices=["full", "tldr", "links"],
        default=None,
        help="Override format for this run (default: use each user's stored format)",
    )
    args = parser.parse_args()

    asyncio.run(run(
        target_telegram_id=args.user,
        force_refresh=args.force_refresh,
        format_override=args.format,
    ))
