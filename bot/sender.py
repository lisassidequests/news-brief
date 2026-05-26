"""
sender.py — chunks the generated brief and sends it to a Telegram user.

Telegram messages have a 4,096 character hard limit.  We avoid hitting it by
sending each numbered story as its own message, then the Strategic Action
Matrix as a final message.  A 1-second delay between messages respects the
Telegram Bot API rate limit of ~30 messages/second globally, but we stay
conservative at 1 msg/s per user to avoid 429 errors.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

logger = logging.getLogger(__name__)

# Telegram hard limit; we use 4000 to leave headroom for Markdown escaping
TELEGRAM_MAX_CHARS = 4000

# Delay between successive messages to the same user (Telegram rate limit)
SEND_DELAY_SECONDS = 1.0

# Singapore Time offset from UTC
SGT_OFFSET_HOURS = 8


def _sgt_now_str() -> str:
    """Return current time formatted as 'DD MMM YYYY, HH:MM SGT'."""
    utc_now = datetime.now(timezone.utc)
    # Manual offset because pytz/zoneinfo adds install complexity
    sgt_hour = (utc_now.hour + SGT_OFFSET_HOURS) % 24
    sgt_date = utc_now  # close enough for the header; date rolls at midnight UTC
    return utc_now.strftime(f"%d %b %Y, {sgt_hour:02d}:%M SGT")


def _split_brief_into_chunks(brief_text: str) -> list[str]:
    """
    Split the brief into sendable chunks.

    Strategy:
      - Each numbered story block (lines starting with "1.", "2.", …) becomes
        its own chunk.
      - The Strategic Action Matrix block becomes the final chunk.
      - Any chunk that still exceeds TELEGRAM_MAX_CHARS is split on paragraph
        boundaries as a fallback.
    """
    chunks: list[str] = []

    # Split on lines that begin a new numbered story (e.g. "1.\n" or "1. ")
    # or on the Strategic Action Matrix header.
    story_pattern = re.compile(r"(?=^\d+\.\s*$)", re.MULTILINE)
    matrix_pattern = re.compile(r"(?=^Strategic Action Matrix)", re.MULTILINE)

    # First, cut off the Action Matrix
    matrix_split = matrix_pattern.split(brief_text, maxsplit=1)
    stories_text = matrix_split[0]
    matrix_text = matrix_split[1] if len(matrix_split) > 1 else ""

    # Split individual stories
    story_blocks = story_pattern.split(stories_text)
    for block in story_blocks:
        block = block.strip()
        if not block:
            continue
        if len(block) <= TELEGRAM_MAX_CHARS:
            chunks.append(block)
        else:
            # Fallback: split on double newlines (paragraph boundaries)
            paragraphs = block.split("\n\n")
            current = ""
            for para in paragraphs:
                if len(current) + len(para) + 2 > TELEGRAM_MAX_CHARS:
                    if current:
                        chunks.append(current.strip())
                    current = para
                else:
                    current = (current + "\n\n" + para) if current else para
            if current:
                chunks.append(current.strip())

    if matrix_text.strip():
        # Action Matrix may itself exceed the limit (unlikely but handled)
        if len(matrix_text) <= TELEGRAM_MAX_CHARS:
            chunks.append(matrix_text.strip())
        else:
            lines = matrix_text.strip().split("\n")
            current = ""
            for line in lines:
                if len(current) + len(line) + 1 > TELEGRAM_MAX_CHARS:
                    if current:
                        chunks.append(current.strip())
                    current = line
                else:
                    current = (current + "\n" + line) if current else line
            if current:
                chunks.append(current.strip())

    return chunks


async def send_brief(telegram_id: int, brief_text: str, bot_token: str) -> None:
    """
    Send the full brief to `telegram_id`, chunked into multiple messages.

    Raises TelegramError on unrecoverable send failure so the caller can log it.
    """
    bot = Bot(token=bot_token)
    date_str = _sgt_now_str()
    header = f"🛡 *Cyber Intel Brief — {date_str}*\n\n"

    chunks = _split_brief_into_chunks(brief_text)
    if not chunks:
        logger.warning("Brief for user %d produced no sendable chunks", telegram_id)
        return

    # Prepend the header to the first message only
    chunks[0] = header + chunks[0]

    async with bot:
        for i, chunk in enumerate(chunks):
            try:
                await bot.send_message(
                    chat_id=telegram_id,
                    text=chunk,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,  # keeps the chat clean
                )
                logger.debug(
                    "Sent chunk %d/%d to user %d (%d chars)",
                    i + 1,
                    len(chunks),
                    telegram_id,
                    len(chunk),
                )
            except TelegramError as exc:
                # Log which chunk failed so we can debug Markdown issues
                logger.error(
                    "TelegramError sending chunk %d to user %d: %s\nChunk preview: %.200s",
                    i + 1,
                    telegram_id,
                    exc,
                    chunk,
                )
                raise  # re-raise so brief_generator.py can log to delivery_log

            # Respect rate limit: 1 message per second per user
            if i < len(chunks) - 1:
                await asyncio.sleep(SEND_DELAY_SECONDS)

        # Feedback prompt after the last brief chunk
        await asyncio.sleep(SEND_DELAY_SECONDS)
        await bot.send_message(
            chat_id=telegram_id,
            text="Was this brief useful?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("👍 Useful",     callback_data="fb:up"),
                InlineKeyboardButton("👎 Not useful", callback_data="fb:down"),
                InlineKeyboardButton("✏️ Refine",     callback_data="fb:refine"),
            ]]),
        )


async def send_text(telegram_id: int, text: str, bot_token: str) -> None:
    """
    Helper for sending short plain-text messages (e.g. admin broadcasts,
    onboarding confirmations triggered by the worker).
    Splits on TELEGRAM_MAX_CHARS if needed.
    """
    bot = Bot(token=bot_token)
    async with bot:
        for start in range(0, len(text), TELEGRAM_MAX_CHARS):
            chunk = text[start : start + TELEGRAM_MAX_CHARS]
            await bot.send_message(
                chat_id=telegram_id,
                text=chunk,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            if start + TELEGRAM_MAX_CHARS < len(text):
                await asyncio.sleep(SEND_DELAY_SECONDS)
