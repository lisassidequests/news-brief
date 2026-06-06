# Cyber & AI Policy Brief Bot

A Telegram bot that delivers a daily AI-generated intelligence brief covering cybersecurity threats and AI governance developments. Built for senior public sector leaders and policy officers who need high-signal, concise reporting without noise.

---

## How It Works

### End-to-End Flow

```
User sends a command
        │
        ▼
Cloudflare Worker (webhook receiver)
  - Authenticates the request
  - Handles commands and button presses
  - Reads/writes user state in Supabase
        │
        ├── Simple commands (e.g. /settings, /pause)
        │   └── Handled entirely in the Worker; Supabase updated; reply sent
        │
        └── Brief requests (/brief, /tldr, /preview)
            │
            ├── Cache hit → send directly from Supabase (instant)
            │
            └── Cache miss → trigger GitHub Actions workflow
                        │
                        ▼
                GitHub Actions (manual_brief.yml)
                  1. Fetch articles (RSS feeds or Supabase article cache)
                  2. Score and prioritise articles
                  3. Call OpenRouter LLM (Claude) to generate brief
                  4. Send to Telegram via python-telegram-bot
                  5. Write generated brief to Supabase cache
```

The **daily brief** (08:00 SGT) follows the same pipeline but runs automatically via `daily_brief.yml` and processes all active users at once.

---

## Article Selection

### Sources

Articles are pulled exclusively from hand-curated RSS feeds — no general news APIs. Every source is either a primary authority or best-in-class journalism.

**Cybersecurity — Government advisories**
- CISA (US critical infrastructure, Known Exploited Vulnerabilities)
- Singapore CSA (advisories and alerts)
- ENISA (EU threat landscape, NIS2, AI Act)

**Cybersecurity — Tier-1 threat intelligence**
- Mandiant, Recorded Future, Cisco Talos, Palo Alto Unit 42

**Cybersecurity — Authoritative journalism**
- KrebsOnSecurity, SecurityWeek, The Hacker News, Bleeping Computer, The Record

**AI news**
- Financial Times Technology, MIT Technology Review (AI), Semafor Technology

**AI policy & governance research**
- Stanford HAI, CSET Georgetown, OECD AI Policy Observatory

**Singapore & EU AI governance**
- AI Verify Foundation, EU AI Office

### Freshness Filter

Only articles published within the **last 24 hours** are included. If fewer articles are available on a given day, the brief covers only what exists — no padding with older content.

### Article Scoring and Prioritisation

Every article is scored before being passed to the LLM:

| Component | Weight |
|---|---|
| Government / primary source | +30 |
| Tier-1 threat intelligence / policy research | +20 |
| Authoritative journalism | +10 |
| Each user topic keyword matched in title/summary | +5 each |
| Published within 12 hours | +10 |
| Published within 24 hours | +5 |

The top-scored articles are selected: **5 for TL;DR**, **20 for full brief**. User topics add a personalisation bonus but do not exclude articles — every user always receives coverage across both cybersecurity and AI.

### Article Cache

Fetched articles are stored in Supabase (`articles` table) for 24 hours. If a second brief run happens within the same day, the cached articles are used instead of re-fetching all RSS feeds. This avoids redundant network calls when multiple users trigger on-demand briefs.

---

## Brief Generation

### LLM

Articles are formatted into a structured prompt and sent to **Claude** (via OpenRouter). The model is instructed to:
- Use only the provided articles — no hallucination
- Include a real, working URL for every story
- Name specific Singapore agencies and legislation where relevant (CSA, IMDA, MAS, Cybersecurity Act, PDPA, ASEAN Digital Masterplan)
- Produce only as many stories as there are verified articles

### Output Structure

Each brief begins with a narrative opening paragraph, then covers each story in this structure:

```
*N. Category: Headline*
_Primary Source: Publication (Date)_
[Read the full article](url)

*The Technical Event:* What happened, in plain English.

*The Policy Impact (Geographic scope):*
Regulatory or strategic implications for the recipient's portfolio.

*The "So What":* One paragraph framing the key implication as a
diagnostic question or action for a senior official. References
specific Singapore agencies or legislation where applicable.
```

### Delivery Formats

| Format | Contents |
|---|---|
| **Full** | Complete brief — all sections per story |
| **TL;DR** | Headline + source + Policy Impact per story |
| **Links** | Headline + article link per story |

Format is set during onboarding and can be changed at any time via `/settings`.

### Brief Cache

After every successful send, the formatted brief is stored in Supabase (`brief_cache` table, keyed by user + format). Subsequent `/brief` or `/tldr` calls that day serve the cached brief instantly — no LLM call, no GitHub Actions run. The cache is refreshed whenever a new brief is generated (daily run, `/preview`, or settings change).

---

## Message Handling

### Cloudflare Worker

The Worker is the only internet-facing component. Every Telegram update arrives here as a POST request.

**Authentication:** Each request must carry the `X-Telegram-Bot-Api-Secret-Token` header matching the configured webhook secret. Requests without it are rejected with 401.

**Update types handled:**
- `message` — text commands and free-text input (onboarding, topic/preference entry)
- `callback_query` — inline keyboard button presses (format selection, feedback, settings)

**User state machine** (tracked in `users.onboarding_step`):

| State | Trigger | Next state |
|---|---|---|
| `awaiting_format` | `/start` for a new user | `awaiting_topics` (after format button pressed) |
| `awaiting_topics` | Format selected or Change Topics tapped | `null` (complete) |
| `awaiting_feedback` | 👎 Not useful / ✏️ Refine / Update Preferences tapped | `null` (after text received) |
| `null` | Normal operation | — |

All database writes from the Worker use `PATCH` (update) except during `/start`, which uses an upsert in case the user is new.

### Sending a Brief

Telegram messages have a 4,096-character hard limit. The Python `sender.py` handles this by:

1. Sending a **📋 Today's Headlines** index message first — all story headlines, their source/date, and article links in one compact block.
2. Splitting the full brief on numbered story boundaries — each story becomes its own message.
3. Falling back to paragraph splits for any story that still exceeds 4,000 characters.
4. Sending a **feedback prompt** (👍 / 👎 / ✏️ Refine) after the last chunk.

A 1-second delay is applied between consecutive messages to respect Telegram's rate limit.

### User Feedback Loop

After every brief the user receives three buttons:

- **👍 Useful** — logs the rating; no other action
- **👎 Not useful** — logs the rating; prompts for free-text feedback
- **✏️ Refine** — prompts for free-text customisation notes

Feedback text is prepended to `users.preferences` (capped at 300 characters). On the next brief generation, users with preferences get an individual LLM call with their preference string injected into the prompt. Users without preferences share a single grouped LLM call — no extra cost.

---

## Commands

### User Commands

| Command | What it does |
|---|---|
| `/start` | Creates your account and walks through format + topic setup |
| `/brief` | Sends your full brief instantly (from cache if available) |
| `/tldr` | Sends your TL;DR brief instantly (from cache if available) |
| `/preview` | If you have saved customisations, generates a fresh personalised brief applying them. If not, explains how to set them and sends the standard brief. |
| `/settings` | Shows your current settings with buttons to change format, topics, or preferences |
| `/pause` | Pauses your daily 8am brief |
| `/resume` | Resumes daily delivery |

### Admin Commands (requires `is_admin = true` in Supabase)

| Command | What it does |
|---|---|
| `/users` | Lists all registered users with format and active status |
| `/logs` | Shows last 7 days of delivery log (success/failure per user) |
| `/broadcast <message>` | Sends a plain-text message to all active users |

---

## Database Schema

| Table | Purpose |
|---|---|
| `users` | One row per registered user: name, format, topics, preferences, timezone, delivery time, active/admin flags, onboarding state |
| `articles` | 24-hour cache of fetched RSS articles (avoids re-fetching on same day) |
| `brief_cache` | Cached formatted brief per (user, format) — serves instant /brief and /tldr |
| `seen_articles` | Which URLs each user has already received (pruned after 7 days) |
| `delivery_log` | Immutable audit trail of every brief send attempt |
| `brief_feedback` | Rating rows (up/down) for analytics |

---

## Setup Guide

### 1. Create a Telegram Bot

1. Message **@BotFather** on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the **Bot Token**
4. Set commands via `/setcommands`:

```
start - Set up your account and daily brief
brief - Get your full cyber and AI brief now
tldr - Get a TL;DR brief now
settings - View or update your preferences
pause - Pause your daily brief delivery
resume - Resume your daily brief delivery
preview - Apply your saved customisations and preview today's brief
```

### 2. Set Up Supabase

1. Go to [supabase.com](https://supabase.com) → **New Project** (choose Singapore region)
2. Open **SQL Editor**, paste the full contents of [`supabase/schema.sql`](supabase/schema.sql), click **Run**
3. Go to **Settings → API** and copy:
   - **Project URL** (e.g. `https://xxxx.supabase.co`)
   - **service_role** key (not the anon key)

### 3. Get an OpenRouter Key

1. Go to [openrouter.ai](https://openrouter.ai) → sign up
2. Navigate to **Keys → Create Key**
3. Add credits (Claude Sonnet costs ~$0.003/1K input tokens; a full run for one user is roughly $0.01–0.03)
4. Copy your API key

### 4. Fork the Repo and Add GitHub Secrets

Go to your fork → **Settings → Secrets and variables → Actions** → add:

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From BotFather |
| `OPENROUTER_API_KEY` | From OpenRouter |
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Supabase service_role key |

### 5. Deploy the Cloudflare Worker

1. Go to [dash.cloudflare.com](https://dash.cloudflare.com) → **Workers & Pages → Create Worker**
2. Paste the contents of `cloudflare/worker.js` into the editor and click **Deploy**
3. Note your worker URL (e.g. `https://cyber-intel-brief-bot.yoursubdomain.workers.dev`)
4. Add secrets via **Settings → Variables and Secrets**:

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | Any random string you choose |
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Supabase service_role key |
| `GITHUB_PAT` | GitHub personal access token (repo + workflow scopes) |
| `GITHUB_REPO_OWNER` | Your GitHub username |
| `GITHUB_REPO_NAME` | Repository name (e.g. `news-brief`) |
| `GITHUB_REF` | Branch to dispatch workflows from (e.g. `main`) |

5. Register the webhook with Telegram (run once):

```bash
curl "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook" \
  -d "url=https://<your-worker>.workers.dev/webhook" \
  -d "secret_token=<YOUR_TELEGRAM_WEBHOOK_SECRET>"
```

Expected response: `{"ok":true,"result":true,"description":"Webhook was set"}`

### 6. Make Yourself an Admin

Run in the Supabase SQL editor (find your Telegram ID via [@userinfobot](https://t.me/userinfobot)):

```sql
UPDATE users SET is_admin = true WHERE telegram_id = 123456789;
```

### 7. Test the Bot

1. Send `/start` to your bot — it should welcome you and ask for format preference
2. Complete setup (format → topics)
3. Send `/brief` — GitHub Actions will run and the brief should arrive within ~60 seconds
4. Check **Actions** tab on GitHub to see the `manual_brief.yml` run

---

## Folder Structure

```
/
├── .github/workflows/
│   ├── daily_brief.yml       # Cron: 08:00 SGT daily, all active users
│   ├── manual_brief.yml      # On-demand: single user via /brief or /preview
│   └── deploy_worker.yml     # Auto-deploys worker.js to Cloudflare on push
├── bot/
│   ├── brief_generator.py    # Main pipeline: articles → LLM → send → cache
│   ├── news_fetcher.py       # RSS fetching, deduplication, 24h filter
│   ├── sender.py             # Message chunking and Telegram delivery
│   └── supabase_client.py    # All database read/write helpers
├── cloudflare/
│   ├── worker.js             # Webhook receiver, command handler, state machine
│   └── wrangler.toml         # Cloudflare Worker config
├── supabase/
│   └── schema.sql            # Full database schema (paste into SQL editor)
└── requirements.txt          # Python dependencies
```

---

## Troubleshooting

**Bot doesn't respond to /start**
- Check Cloudflare Worker logs: Dashboard → Workers → your worker → Logs
- Verify the webhook: `https://api.telegram.org/bot<TOKEN>/getWebhookInfo`
- Confirm `TELEGRAM_WEBHOOK_SECRET` matches what you used in the `setWebhook` call

**Brief never arrives after /brief**
- Check the GitHub Actions tab for a `manual_brief.yml` run
- If no run appears, the Worker failed to call the GitHub API — check `GITHUB_PAT` scopes and `GITHUB_REF`
- If the run failed, the error log will show which step failed (article fetch, LLM call, or Telegram send)

**Brief is always the same (cache not refreshing)**
- Use `/preview` to force a fresh generation with your saved preferences
- Or trigger a manual run with `force_refresh=true` from the GitHub Actions UI

**Articles are too old**
- The article cache has a 24-hour freshness window. If all RSS feeds returned nothing recent, the brief will be sparse — this is by design. Check the Actions log for `RSS <source>: 0 entries fetched` warnings to identify feeds that may be down.

**"null value in column name" error in Worker logs**
- This means a button press tried to upsert a user row without a name. All post-onboarding updates should use PATCH. Check that the deployed Worker is the latest version.
