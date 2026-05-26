# Cyber Intel Brief Bot

A Telegram bot that delivers a daily AI-generated cybersecurity intelligence brief to subscribers. Built for senior public sector leaders who need concise, high-signal reporting on cyber threats, AI governance, and supply chain risks.

## Architecture

```
Telegram ──webhook──▶ Cloudflare Worker ──▶ Supabase (user state)
                             │
                             └──▶ GitHub API (workflow_dispatch)
                                        │
                             GitHub Actions (cron / on-demand)
                                        │
                          news_fetcher.py ──▶ NewsAPI + RSS feeds
                                        │
                         brief_generator.py ──▶ OpenRouter (Claude)
                                        │
                              sender.py ──▶ Telegram Bot API
```

All services used are **free tier**. No Docker. No compiled Python extensions.

---

## Step-by-Step Setup

### 1. Create a Telegram Bot

1. Open Telegram and message **@BotFather**
2. Send `/newbot` and follow the prompts (name + username)
3. Copy the **Bot Token** (looks like `123456789:AABBcc...`)
4. Optionally disable group joins: `/setjoingroups` → `Disable`

---

### 2. Set Up Supabase

1. Go to [supabase.com](https://supabase.com) → **New Project**
2. Choose a name, password, and the **Singapore** region (closest to SGT users)
3. Once the project is ready, open the **SQL Editor**
4. Paste the entire contents of [`supabase/schema.sql`](supabase/schema.sql) and click **Run**
5. Go to **Settings → API** and copy:
   - **Project URL** (e.g. `https://xxxx.supabase.co`)
   - **service_role** key (under "Project API keys" — use this, not the anon key)

---

### 3. Get a NewsAPI Key

1. Go to [newsapi.org](https://newsapi.org) → **Get API Key**
2. Sign up for a free account
3. Copy your API key from the dashboard

> Free tier: 100 requests/day, articles up to 1 month old. Sufficient for 1 daily run.

---

### 4. Get an OpenRouter Key

1. Go to [openrouter.ai](https://openrouter.ai) → sign up
2. Navigate to **Keys** → **Create Key**
3. Add credits ($5 minimum — `claude-sonnet-4-6` costs ~$0.003/1K input tokens)
4. Copy your API key

---

### 5. Fork/Clone the Repo and Add GitHub Secrets

1. Fork this repository to your own GitHub account
2. Go to your fork → **Settings → Secrets and variables → Actions**
3. Add the following secrets (one by one, using **New repository secret**):

| Secret name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From BotFather |
| `OPENROUTER_API_KEY` | From OpenRouter |
| `NEWSAPI_KEY` | From NewsAPI |
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Supabase service_role key |

> The `TELEGRAM_WEBHOOK_SECRET`, `GITHUB_PAT`, `GITHUB_REPO_OWNER`, and `GITHUB_REPO_NAME` are used by the Cloudflare Worker — they go in Wrangler secrets (next step), not GitHub.

---

### 6. Deploy the Cloudflare Worker

#### Prerequisites

```bash
npm install -g wrangler
wrangler login
```

#### Create the worker

```bash
cd cloudflare
wrangler init cyber-brief-worker --no-bundle
# When prompted, choose "Hello World" worker template
# Replace the generated index.js with worker.js contents
```

Or if you already have a `wrangler.toml`:

```bash
wrangler deploy worker.js
```

#### Set worker secrets

Run each of these and paste the value when prompted:

```bash
wrangler secret put TELEGRAM_BOT_TOKEN
wrangler secret put TELEGRAM_WEBHOOK_SECRET    # choose any random string
wrangler secret put SUPABASE_URL
wrangler secret put SUPABASE_SERVICE_KEY
wrangler secret put GITHUB_PAT                 # needs repo + workflow scopes
wrangler secret put GITHUB_REPO_OWNER          # your GitHub username
wrangler secret put GITHUB_REPO_NAME           # news-brief (or your fork name)
```

#### Register the webhook with Telegram

Replace the placeholders and run this once:

```bash
curl "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook" \
  -d "url=https://<your-worker>.workers.dev" \
  -d "secret_token=<YOUR_TELEGRAM_WEBHOOK_SECRET>"
```

You should get: `{"ok":true,"result":true,"description":"Webhook was set"}`

---

### 7. Test the Bot

1. Open Telegram and find your bot by its username
2. Send `/start` — the bot should reply with a welcome message and ask for your name
3. Complete the onboarding (name → format → topics)
4. Send `/brief` — the bot will trigger a GitHub Actions run and send a brief within ~60 seconds
5. Check **Actions** tab on GitHub to see the `manual_brief.yml` run

---

## Folder Structure

```
/
├── .github/
│   └── workflows/
│       ├── daily_brief.yml       # Cron: 8am SGT daily (all users)
│       └── manual_brief.yml      # On-demand: single user via /brief
├── bot/
│   ├── brief_generator.py        # Main pipeline: fetch → LLM → send
│   ├── news_fetcher.py           # NewsAPI + RSS article fetching
│   ├── sender.py                 # Chunks and sends Telegram messages
│   └── supabase_client.py        # DB read/write helpers
├── cloudflare/
│   └── worker.js                 # Webhook receiver + command handler
├── supabase/
│   └── schema.sql                # Paste into Supabase SQL editor
├── requirements.txt              # Python deps (pure-Python, no binaries)
├── .env.example                  # Template for local dev
└── README.md
```

---

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Onboarding flow: name, format, topics |
| `/brief` | Generate and send a brief right now |
| `/preview` | Same as `/brief` with a note that your scheduled brief still sends |
| `/settings` | View and change your preferences |
| `/pause` | Stop receiving daily briefs |
| `/resume` | Resume daily briefs |

### Admin Commands (set `is_admin = true` in Supabase)

| Command | Description |
|---|---|
| `/users` | List all registered users and their settings |
| `/logs` | Show last 7 days of delivery log |
| `/broadcast <message>` | Send a plain-text message to all active users |

---

## Delivery Formats

| Format | What you get |
|---|---|
| `full` | Complete brief with all 5 sections per story + Action Matrix |
| `tldr` | Headline + Policy Impact per story + Action Matrix |
| `links` | Headline + URL per story + Action Matrix |

---

## Troubleshooting

**Bot doesn't respond to /start**
- Check the Cloudflare Worker logs in the dashboard (Workers → your worker → Logs)
- Verify the webhook is registered: `https://api.telegram.org/bot<TOKEN>/getWebhookInfo`
- Confirm `TELEGRAM_WEBHOOK_SECRET` matches what you set in the webhook URL

**Brief never arrives after /brief**
- Check GitHub Actions tab — look for a `manual_brief.yml` run
- If the run failed, click it to see the error log
- Common issues: wrong `SUPABASE_URL`, expired `NEWSAPI_KEY`, OpenRouter out of credits

**"Telegram API sendMessage failed" in GitHub logs**
- The user may have blocked the bot or deleted the conversation
- Check `delivery_log` table in Supabase for the error message

**NewsAPI returns 0 articles**
- Free tier limits to the last 30 days and 100 requests/day
- Check your API key is active at newsapi.org

---

## Making Yourself an Admin

Run this in the Supabase SQL editor (replace with your real Telegram ID):

```sql
UPDATE users SET is_admin = true WHERE telegram_id = 123456789;
```

To find your Telegram ID, message [@userinfobot](https://t.me/userinfobot) on Telegram.
