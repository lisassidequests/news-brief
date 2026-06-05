-- =============================================================================
-- Cyber Intel Brief Bot — Supabase Schema
-- Paste this entire file into the Supabase SQL editor and click "Run".
-- =============================================================================

-- ---------------------------------------------------------------------------
-- ENUM: delivery format preference
-- ---------------------------------------------------------------------------
CREATE TYPE delivery_format AS ENUM ('full', 'tldr', 'links');

-- ---------------------------------------------------------------------------
-- TABLE: users
-- One row per Telegram user who has run /start.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    telegram_id     BIGINT PRIMARY KEY,          -- Telegram user ID (unique per account)
    name            TEXT NOT NULL,               -- Display name supplied during onboarding
    timezone        TEXT NOT NULL DEFAULT 'Asia/Singapore',
    delivery_time   TIME NOT NULL DEFAULT '08:00:00',
    format          delivery_format NOT NULL DEFAULT 'tldr',
    topics          JSONB NOT NULL DEFAULT '[]', -- e.g. ["CISA","supply chain","AI governance"]
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    is_admin        BOOLEAN NOT NULL DEFAULT FALSE,
    onboarding_step TEXT,                        -- Tracks partial /start flow; NULL when complete
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for quick lookup of all active users during cron runs
CREATE INDEX IF NOT EXISTS idx_users_active ON users (is_active) WHERE is_active = TRUE;

-- ---------------------------------------------------------------------------
-- TABLE: seen_articles
-- Tracks which URLs each user has already received so we don't repeat them.
-- Rows older than 7 days are pruned at the start of every brief run.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS seen_articles (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users (telegram_id) ON DELETE CASCADE,
    article_url TEXT NOT NULL,
    seen_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, article_url)               -- Prevent duplicate entries
);

-- Index for fast pruning of old rows and deduplication lookups
CREATE INDEX IF NOT EXISTS idx_seen_articles_user   ON seen_articles (user_id);
CREATE INDEX IF NOT EXISTS idx_seen_articles_seen_at ON seen_articles (seen_at);

-- ---------------------------------------------------------------------------
-- TABLE: delivery_log
-- Immutable audit trail of every attempted brief send.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS delivery_log (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES users (telegram_id) ON DELETE CASCADE,
    sent_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status        TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    article_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT                          -- NULL on success, error string on failure
);

-- Index for the /logs admin command (last 7 days filter)
CREATE INDEX IF NOT EXISTS idx_delivery_log_user    ON delivery_log (user_id);
CREATE INDEX IF NOT EXISTS idx_delivery_log_sent_at ON delivery_log (sent_at);

-- ---------------------------------------------------------------------------
-- TABLE: brief_cache
-- One row per (user, format). Updated by every successful brief send.
-- /brief and /tldr read from here first — no LLM call if cache is warm.
-- Invalidated automatically when the user changes topics or format
-- (the next workflow run generates a fresh brief and overwrites the row).
-- To add to an existing deployment, paste only this block.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS brief_cache (
    user_id      BIGINT NOT NULL REFERENCES users (telegram_id) ON DELETE CASCADE,
    format       TEXT NOT NULL CHECK (format IN ('full', 'tldr', 'links')),
    brief_text   TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, format)
);

-- ---------------------------------------------------------------------------
-- TABLE: brief_feedback
-- Immutable audit trail of user ratings on delivered briefs.
-- Used for analytics; individual preferences are stored on users.preferences.
-- To add to an existing deployment, paste only this block into the SQL editor.
-- ---------------------------------------------------------------------------
ALTER TABLE users ADD COLUMN IF NOT EXISTS preferences TEXT;

CREATE TABLE IF NOT EXISTS brief_feedback (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users (telegram_id) ON DELETE CASCADE,
    rating     TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_brief_feedback_user ON brief_feedback (user_id);

-- ---------------------------------------------------------------------------
-- TABLE: articles
-- Short-lived cache of fetched article content.
-- Avoids re-hitting NewsAPI and RSS feeds when re-running brief generation
-- (e.g. after tweaking the prompt or format) within the same day.
-- Rows older than 48 hours are pruned at the start of each brief run.
-- To add this table to an existing deployment, paste only this block into
-- the Supabase SQL editor — no need to re-run the full schema.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS articles (
    id           BIGSERIAL PRIMARY KEY,
    url          TEXT UNIQUE NOT NULL,    -- Natural dedup key
    title        TEXT NOT NULL,
    source       TEXT NOT NULL,
    published_at TIMESTAMPTZ,            -- NULL when source didn't supply a date
    summary      TEXT,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()  -- When we last fetched this article
);

-- Index for cache-freshness queries and pruning (both filter on fetched_at)
CREATE INDEX IF NOT EXISTS idx_articles_fetched_at ON articles (fetched_at);

-- ---------------------------------------------------------------------------
-- Row-level security: disabled intentionally.
-- The bot accesses Supabase exclusively via the service-role key from
-- GitHub Actions and the Cloudflare Worker — no end-user JWT auth.
-- If you want RLS, enable it and add service-role bypass policies.
-- ---------------------------------------------------------------------------
