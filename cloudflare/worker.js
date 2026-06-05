/**
 * worker.js — Cloudflare Worker: Telegram webhook receiver and command handler.
 *
 * Responsibilities:
 *   - Verify the Telegram webhook secret on every inbound request
 *   - Parse Telegram Update objects (messages + callback_query for inline buttons)
 *   - Handle bot commands: /start, /settings, /brief, /pause, /resume, /preview
 *   - Handle admin commands: /users, /logs, /broadcast
 *   - Read/write Supabase via the REST API using fetch() — no JS SDK
 *   - Trigger GitHub Actions workflow_dispatch for /brief and /preview
 *
 * Environment variables (set via `wrangler secret put` or the Cloudflare dashboard):
 *   TELEGRAM_BOT_TOKEN       — from BotFather
 *   TELEGRAM_WEBHOOK_SECRET  — arbitrary string you set when registering webhook
 *   SUPABASE_URL             — e.g. https://xxxx.supabase.co
 *   SUPABASE_SERVICE_KEY     — service role key (bypasses RLS)
 *   GITHUB_PAT               — personal access token with workflow scope
 *   GITHUB_REPO_OWNER        — GitHub username or org
 *   GITHUB_REPO_NAME         — repository name (e.g. news-brief)
 *   GITHUB_REF               — branch to dispatch workflows from (default: "main")
 */

// =============================================================================
// Entry point
// =============================================================================

export default {
  async fetch(request, env) {
    // Only accept POST from Telegram
    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    // Verify the secret token Telegram sends in X-Telegram-Bot-Api-Secret-Token
    const incomingSecret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (incomingSecret !== env.TELEGRAM_WEBHOOK_SECRET) {
      return new Response("Unauthorized", { status: 401 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("Bad Request", { status: 400 });
    }

    // Dispatch based on update type
    try {
      if (update.callback_query) {
        await handleCallbackQuery(update.callback_query, env);
      } else if (update.message) {
        await handleMessage(update.message, env);
      }
    } catch (err) {
      // Log to Cloudflare but still return 200 so Telegram doesn't retry
      console.error("Handler error:", err);
    }

    // Telegram expects a 200 response; anything else triggers retries
    return new Response("OK", { status: 200 });
  },
};

// =============================================================================
// Message router
// =============================================================================

async function handleMessage(message, env) {
  const chatId = message.chat.id;
  const text = (message.text || "").trim();

  if (!text.startsWith("/")) {
    // Non-command text — check if user is mid-onboarding
    await handleFreeText(message, env);
    return;
  }

  // Extract command (strip bot username suffix like /start@MyBot)
  const command = text.split("@")[0].split(" ")[0].toLowerCase();
  const args = text.slice(command.length).trim();

  switch (command) {
    case "/start":
      await handleStart(message, env);
      break;
    case "/settings":
      await handleSettings(message, env);
      break;
    case "/brief":
      await handleBrief(message, env, "full", false);
      break;
    case "/tldr":
      await handleBrief(message, env, "tldr", false);
      break;
    case "/preview":
      await handleBrief(message, env, "full", true);
      break;
    case "/pause":
      await handlePause(message, env);
      break;
    case "/resume":
      await handleResume(message, env);
      break;
    // Admin commands
    case "/users":
      await handleAdminUsers(message, env);
      break;
    case "/logs":
      await handleAdminLogs(message, env);
      break;
    case "/broadcast":
      await handleAdminBroadcast(message, args, env);
      break;
    default:
      await sendMessage(chatId, "Unknown command. Try /start, /brief, /tldr, /settings, /pause, or /resume.", env);
  }
}

// =============================================================================
// /start — multi-step onboarding
// =============================================================================

async function handleStart(message, env) {
  const chatId = message.chat.id;
  const telegramId = message.from.id;
  const name = message.from.first_name || "there";

  // Check if user already exists and has completed onboarding
  const existing = await getUser(telegramId, env);
  if (existing && !existing.onboarding_step) {
    await sendMessage(
      chatId,
      `Welcome back, *${existing.name}*! 👋\n\nYour brief is scheduled for ${existing.delivery_time} SGT.\n\nUse /settings to change preferences or /brief for an instant brief.`,
      env
    );
    return;
  }

  // Create/reset user record using Telegram name, skip to format selection
  await upsertUser(
    {
      telegram_id: telegramId,
      name,
      onboarding_step: "awaiting_format",
      is_active: true,
      is_admin: false,
    },
    env
  );

  await sendMessageWithKeyboard(
    chatId,
    `👋 Hi *${name}*! Welcome to the *Cyber Intel Brief Bot*.\n\nI deliver a daily intelligence brief on cybersecurity, AI governance, and supply chain threats — tailored to your interests.\n\nHow would you like to receive your brief?`,
    [
      [
        { text: "📄 Full Brief", callback_data: "fmt:full" },
        { text: "⚡ TL;DR", callback_data: "fmt:tldr" },
        { text: "🔗 Links Only", callback_data: "fmt:links" },
      ],
    ],
    env
  );
}

// =============================================================================
// Free-text handler — drives onboarding state machine
// =============================================================================

async function handleFreeText(message, env) {
  const chatId = message.chat.id;
  const telegramId = message.from.id;
  const text = message.text || "";

  const user = await getUser(telegramId, env);
  if (!user) {
    await sendMessage(chatId, "Use /start to set up your brief.", env);
    return;
  }
  if (!user.onboarding_step) {
    await sendMessage(
      chatId,
      "Here's what I can do:\n\n" +
      "/brief — get your full brief now\n" +
      "/tldr — get a TL;DR brief now\n" +
      "/settings — view or change your preferences\n" +
      "/pause — pause daily delivery\n" +
      "/resume — resume daily delivery",
      env
    );
    return;
  }

  switch (user.onboarding_step) {
    case "awaiting_topics": {
      // User typed topics as free text (comma-separated)
      const topics = text
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean)
        .slice(0, 10); // cap at 10 topics

      await upsertUser(
        { telegram_id: telegramId, topics: JSON.stringify(topics), onboarding_step: null },
        env
      );

      const user2 = await getUser(telegramId, env);
      await sendMessage(
        chatId,
        `✅ *All set, ${user2.name}!*\n\n` +
          `📋 Format: *${user2.format.toUpperCase()}*\n` +
          `🏷 Topics: ${topics.length ? topics.join(", ") : "All cyber news"}\n` +
          `🕗 Delivery: *8:00 AM SGT* daily\n\n` +
          `Generating your first brief now — it'll arrive in about 60 seconds.`,
        env
      );
      await triggerGitHubWorkflow(telegramId, user2.format || "tldr", env);
      break;
    }

    case "awaiting_feedback": {
      if (text.trim().toLowerCase() === "clear") {
        await upsertUser({ telegram_id: telegramId, preferences: null, onboarding_step: null }, env);
        await sendMessage(chatId, "✅ Preferences cleared.", env);
      } else {
        const existing = user.preferences || "";
        const combined = (text.trim() + (existing ? " | " + existing : "")).slice(0, 300);
        await upsertUser({ telegram_id: telegramId, preferences: combined, onboarding_step: null }, env);
        await sendMessage(chatId, "✅ Saved — I'll apply this to your next brief.", env);
      }
      break;
    }

    default:
      await sendMessage(chatId, "Use /start to configure your brief.", env);
  }
}

// =============================================================================
// Callback query handler — processes inline keyboard button presses
// =============================================================================

async function handleCallbackQuery(callbackQuery, env) {
  const chatId = callbackQuery.message.chat.id;
  const telegramId = callbackQuery.from.id;
  const data = callbackQuery.data || "";

  // Acknowledge the callback to stop Telegram's loading spinner.
  // Non-fatal — a failure here must not block the response.
  try { await answerCallbackQuery(callbackQuery.id, env); } catch {}

  try {

  if (data.startsWith("fmt:")) {
    const fmt = data.slice(4); // "full", "tldr", or "links"
    await upsertUser(
      { telegram_id: telegramId, format: fmt, onboarding_step: "awaiting_topics" },
      env
    );
    await sendMessage(
      chatId,
      `✅ Format set to *${fmt.toUpperCase()}*.\n\nWhat topics interest you most? Reply with a comma-separated list.\n\n_Examples: CISA, ransomware, AI governance, supply chain, Singapore_\n\n(Or type "all" for everything)`,
      env
    );
  } else if (data === "pause:confirm") {
    await setUserActive(telegramId, false, env);
    await sendMessage(chatId, "⏸ Brief delivery paused. Use /resume to re-activate.", env);
  } else if (data === "resume:confirm") {
    await setUserActive(telegramId, true, env);
    await sendMessage(chatId, "▶️ Brief delivery resumed! You'll receive your next brief at 8:00 AM SGT.", env);
  } else if (data.startsWith("set:fmt:")) {
    const fmt = data.slice(8); // "full", "tldr", or "links"
    await upsertUser({ telegram_id: telegramId, format: fmt, onboarding_step: null }, env);
    await sendMessage(chatId, `✅ Format updated to *${fmt.toUpperCase()}*. Generating your updated brief…`, env);
    await triggerGitHubWorkflow(telegramId, fmt, env);
  } else if (data === "set:topics") {
    await upsertUser({ telegram_id: telegramId, onboarding_step: "awaiting_topics" }, env);
    await sendMessage(
      chatId,
      "What topics would you like to follow? Reply with a comma-separated list.\n\n" +
      "_Example: CISA, ransomware, AI governance, supply chain, Singapore_\n\n" +
      "(Or type \"all\" for everything)",
      env
    );
  } else if (data === "set:prefs") {
    const user = await getUser(telegramId, env);
    const current = user?.preferences ? `\n\nCurrent note: _"${user.preferences}"_` : "";
    await upsertUser({ telegram_id: telegramId, onboarding_step: "awaiting_feedback" }, env);
    await sendMessage(
      chatId,
      `What would make your brief better?${current}\n\n` +
      `_New text is added to your existing note. Reply "clear" to reset._`,
      env
    );
  } else if (data === "fb:up") {
    await saveFeedback(telegramId, "up", env);
    await sendMessage(chatId, "👍 Thanks — glad it was useful!", env);
  } else if (data === "fb:down") {
    await saveFeedback(telegramId, "down", env);
    await upsertUser({ telegram_id: telegramId, onboarding_step: "awaiting_feedback" }, env);
    await sendMessage(chatId,
      "What was off with today's brief? Tell me in your own words.\n\n" +
      "_Example: \"Too US-centric. I need more APAC coverage and shorter summaries.\"_",
      env);
  } else if (data === "fb:refine") {
    const user = await getUser(telegramId, env);
    const current = user?.preferences
      ? `\n\nCurrent note: _"${user.preferences}"_`
      : "";
    await upsertUser({ telegram_id: telegramId, onboarding_step: "awaiting_feedback" }, env);
    await sendMessage(chatId,
      `What would make your brief better?${current}\n\n` +
      `_New text is added to your existing note. Reply "clear" to reset._`,
      env);
  }

  } catch (err) {
    console.error("Callback handler error for data=", data, err);
    try { await sendMessage(chatId, "Something went wrong — please try again.", env); } catch {}
  }
}

// =============================================================================
// /settings — show current settings with action buttons
// =============================================================================

async function handleSettings(message, env) {
  const chatId = message.chat.id;
  const telegramId = message.from.id;

  const user = await getUser(telegramId, env);
  if (!user) {
    await sendMessage(chatId, "No account found. Run /start to set up your brief.", env);
    return;
  }

  await sendSettingsMenu(chatId, telegramId, user, env);
}

async function sendSettingsMenu(chatId, telegramId, user, env) {
  const topics = Array.isArray(user.topics) ? user.topics : JSON.parse(user.topics || "[]");
  const statusEmoji = user.is_active ? "✅ Active" : "⏸ Paused";
  const fmtLabel = { full: "📄 Full", tldr: "⚡ TL;DR", links: "🔗 Links" }[user.format] || user.format.toUpperCase();

  await sendMessageWithKeyboard(
    chatId,
    `⚙️ *Your Current Settings*\n\n` +
      `📋 Format: *${fmtLabel}*\n` +
      `🏷 Topics: ${topics.length ? topics.join(", ") : "All cyber news"}\n` +
      `🕗 Delivery: 8:00 AM SGT daily\n` +
      `📊 Status: ${statusEmoji}\n` +
      `📝 Preferences: ${user.preferences || "None"}`,
    [
      [
        { text: "📄 Full Brief", callback_data: "set:fmt:full" },
        { text: "⚡ TL;DR",     callback_data: "set:fmt:tldr" },
        { text: "🔗 Links Only", callback_data: "set:fmt:links" },
      ],
      [
        { text: "🏷 Change Topics",      callback_data: "set:topics" },
        { text: "📝 Update Preferences", callback_data: "set:prefs" },
      ],
    ],
    env
  );
  // Clear any stale onboarding state so free text isn't misinterpreted
  await upsertUser({ telegram_id: telegramId, onboarding_step: null }, env);
}

// =============================================================================
// /brief and /preview — check cache, then trigger GitHub Actions if needed
// =============================================================================

async function handleBrief(message, env, format, isPreview) {
  const chatId = message.chat.id;
  const telegramId = message.from.id;

  const user = await getUser(telegramId, env);
  if (!user) {
    await sendMessage(chatId, "Run /start first to set up your account.", env);
    return;
  }

  // Serve from cache for non-preview requests
  if (!isPreview) {
    const effectiveFormat = format || user.format || "tldr";
    const cached = await getBriefCache(telegramId, effectiveFormat, env);
    if (cached) {
      await sendBriefChunks(chatId, cached, env);
      return;
    }
  }

  const prefix = isPreview
    ? "⚡ *Preview* — your scheduled brief still sends at 8am SGT.\n\n"
    : "";

  await sendMessage(
    chatId,
    `${prefix}⏳ Generating your brief now… This takes about 30-60 seconds.`,
    env
  );

  const triggered = await triggerGitHubWorkflow(telegramId, format, env);
  if (!triggered) {
    await sendMessage(
      chatId,
      "❌ Failed to trigger brief generation. Please try again in a minute.",
      env
    );
  }
}

async function getBriefCache(userId, format, env) {
  try {
    const data = await supabaseRequest(
      "GET",
      `brief_cache?user_id=eq.${userId}&format=eq.${format}&select=brief_text`,
      null,
      env
    );
    return Array.isArray(data) && data.length > 0 ? data[0].brief_text : null;
  } catch {
    return null;
  }
}

async function sendBriefChunks(chatId, briefText, env) {
  const now = new Date();
  const sgtHour = (now.getUTCHours() + 8) % 24;
  const sgtMinute = String(now.getUTCMinutes()).padStart(2, "0");
  const dateStr = now.toLocaleDateString("en-SG", {
    timeZone: "Asia/Singapore",
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
  const header = `🛡 *Cyber Intel Brief — ${dateStr}, ${String(sgtHour).padStart(2, "0")}:${sgtMinute} SGT*\n\n`;

  const TELEGRAM_MAX = 4000;

  // Split on numbered story starts (mirrors sender.py logic)
  const blocks = briefText.split(/(?=^\*?\d+\.[ \t])/m).filter((b) => b.trim());
  const chunks = [];

  for (const block of blocks) {
    const trimmed = block.trim();
    if (trimmed.length <= TELEGRAM_MAX) {
      chunks.push(trimmed);
    } else {
      const paras = trimmed.split("\n\n");
      let current = "";
      for (const para of paras) {
        if (current.length + para.length + 2 > TELEGRAM_MAX) {
          if (current) chunks.push(current.trim());
          current = para;
        } else {
          current = current ? current + "\n\n" + para : para;
        }
      }
      if (current) chunks.push(current.trim());
    }
  }

  if (!chunks.length) return;
  chunks[0] = header + chunks[0];

  for (let i = 0; i < chunks.length; i++) {
    await sendMessage(chatId, chunks[i], env);
    if (i < chunks.length - 1) await sleep(1000);
  }

  await sleep(1000);
  await sendMessageWithKeyboard(
    chatId,
    "Was this brief useful?",
    [[
      { text: "👍 Useful", callback_data: "fb:up" },
      { text: "👎 Not useful", callback_data: "fb:down" },
      { text: "✏️ Refine", callback_data: "fb:refine" },
    ]],
    env
  );
}

// =============================================================================
// /pause and /resume
// =============================================================================

async function handlePause(message, env) {
  const chatId = message.chat.id;
  await sendMessageWithKeyboard(
    chatId,
    "⏸ Are you sure you want to pause your daily brief?",
    [[
      { text: "Yes, pause it", callback_data: "pause:confirm" },
      { text: "No, keep it active", callback_data: "resume:confirm" },
    ]],
    env
  );
}

async function handleResume(message, env) {
  const chatId = message.chat.id;
  const telegramId = message.from.id;
  await setUserActive(telegramId, true, env);
  await sendMessage(chatId, "▶️ Brief delivery resumed! You'll receive your next brief at 8:00 AM SGT.", env);
}

// =============================================================================
// Admin commands (is_admin = true only)
// =============================================================================

async function requireAdmin(message, env) {
  const user = await getUser(message.from.id, env);
  if (!user || !user.is_admin) {
    await sendMessage(message.chat.id, "⛔ Admin access required.", env);
    return false;
  }
  return true;
}

async function handleAdminUsers(message, env) {
  if (!await requireAdmin(message, env)) return;

  const users = await getAllUsers(env);
  if (!users.length) {
    await sendMessage(message.chat.id, "No users registered yet.", env);
    return;
  }

  const lines = users.map((u) => {
    const fmt = u.format || "full";
    const status = u.is_active ? "✅" : "⏸";
    return `${status} *${u.name}* (ID: ${u.telegram_id}) — ${fmt.toUpperCase()}`;
  });

  const chunks = chunkArray(lines, 20); // 20 users per message
  for (const chunk of chunks) {
    await sendMessage(message.chat.id, `👥 *Registered Users*\n\n${chunk.join("\n")}`, env);
  }
}

async function handleAdminLogs(message, env) {
  if (!await requireAdmin(message, env)) return;

  const logs = await getRecentLogs(env);
  if (!logs.length) {
    await sendMessage(message.chat.id, "No delivery logs in the last 7 days.", env);
    return;
  }

  const lines = logs.slice(0, 30).map((l) => {
    const emoji = l.status === "success" ? "✅" : "❌";
    const date = new Date(l.sent_at).toLocaleString("en-SG", { timeZone: "Asia/Singapore" });
    const err = l.error_message ? ` — ${l.error_message.slice(0, 50)}` : "";
    return `${emoji} User ${l.user_id} | ${date} | ${l.article_count} articles${err}`;
  });

  await sendMessage(message.chat.id, `📊 *Recent Delivery Logs*\n\n${lines.join("\n")}`, env);
}

async function handleAdminBroadcast(message, text, env) {
  if (!await requireAdmin(message, env)) return;
  if (!text) {
    await sendMessage(message.chat.id, "Usage: /broadcast <message>", env);
    return;
  }

  const users = await getAllUsers(env);
  const active = users.filter((u) => u.is_active);

  await sendMessage(
    message.chat.id,
    `📢 Broadcasting to ${active.length} active user(s)…`,
    env
  );

  let sent = 0;
  let failed = 0;
  for (const user of active) {
    try {
      await sendMessage(user.telegram_id, `📢 *Broadcast message:*\n\n${text}`, env);
      sent++;
    } catch {
      failed++;
    }
    // Rate limit: 1 msg/s
    await sleep(1000);
  }

  await sendMessage(
    message.chat.id,
    `✅ Broadcast complete: ${sent} sent, ${failed} failed.`,
    env
  );
}

// =============================================================================
// GitHub Actions workflow_dispatch trigger
// =============================================================================

async function triggerGitHubWorkflow(telegramId, format, env) {
  const url = `https://api.github.com/repos/${env.GITHUB_REPO_OWNER}/${env.GITHUB_REPO_NAME}/actions/workflows/manual_brief.yml/dispatches`;

  const inputs = { telegram_id: String(telegramId) };
  if (format) inputs.format = format;

  const body = {
    ref: env.GITHUB_REF || "main",
    inputs,
  };

  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_PAT}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "CyberBriefBot/1.0",
      },
      body: JSON.stringify(body),
    });

    // GitHub returns 204 No Content on success
    if (resp.status === 204) return true;

    const responseText = await resp.text();
    console.error(`GitHub dispatch failed: HTTP ${resp.status} — ${responseText}`);
    return false;
  } catch (err) {
    console.error("GitHub workflow dispatch failed:", err);
    return false;
  }
}

// =============================================================================
// Supabase REST helpers
// =============================================================================

async function supabaseRequest(method, path, body, env) {
  const url = `${env.SUPABASE_URL}/rest/v1/${path}`;
  const prefer = path.includes("on_conflict")
    ? "resolution=merge-duplicates,return=representation"
    : "return=representation";
  const headers = {
    apikey: env.SUPABASE_SERVICE_KEY,
    Authorization: `Bearer ${env.SUPABASE_SERVICE_KEY}`,
    "Content-Type": "application/json",
    Prefer: prefer,
  };

  const resp = await fetch(url, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  if (!resp.ok) {
    const errText = await resp.text();
    throw new Error(`Supabase ${method} ${path} → ${resp.status}: ${errText}`);
  }

  const text = await resp.text();
  return text ? JSON.parse(text) : null;
}

async function getUser(telegramId, env) {
  const data = await supabaseRequest(
    "GET",
    `users?telegram_id=eq.${telegramId}&select=*`,
    null,
    env
  );
  return Array.isArray(data) && data.length > 0 ? data[0] : null;
}

async function upsertUser(userData, env) {
  return supabaseRequest("POST", "users?on_conflict=telegram_id", userData, env);
}

async function setUserActive(telegramId, isActive, env) {
  return supabaseRequest(
    "PATCH",
    `users?telegram_id=eq.${telegramId}`,
    { is_active: isActive },
    env
  );
}

async function getAllUsers(env) {
  const data = await supabaseRequest("GET", "users?select=*", null, env);
  return Array.isArray(data) ? data : [];
}

async function saveFeedback(userId, rating, env) {
  await supabaseRequest("POST", "brief_feedback", { user_id: userId, rating }, env);
}

async function getRecentLogs(env) {
  const sevenDaysAgo = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString();
  const data = await supabaseRequest(
    "GET",
    `delivery_log?sent_at=gte.${sevenDaysAgo}&order=sent_at.desc&select=*`,
    null,
    env
  );
  return Array.isArray(data) ? data : [];
}

// =============================================================================
// Telegram Bot API helpers
// =============================================================================

async function telegramApi(method, payload, env) {
  const url = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`;
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const err = await resp.text();
    throw new Error(`Telegram API ${method} failed: ${err}`);
  }
  return resp.json();
}

async function sendMessage(chatId, text, env) {
  return telegramApi(
    "sendMessage",
    {
      chat_id: chatId,
      text,
      parse_mode: "Markdown",
      disable_web_page_preview: true,
    },
    env
  );
}

async function sendMessageWithKeyboard(chatId, text, inlineKeyboard, env) {
  return telegramApi(
    "sendMessage",
    {
      chat_id: chatId,
      text,
      parse_mode: "Markdown",
      reply_markup: { inline_keyboard: inlineKeyboard },
    },
    env
  );
}

async function answerCallbackQuery(callbackQueryId, env) {
  return telegramApi("answerCallbackQuery", { callback_query_id: callbackQueryId }, env);
}

// =============================================================================
// Utility
// =============================================================================

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function chunkArray(arr, size) {
  const chunks = [];
  for (let i = 0; i < arr.length; i += size) {
    chunks.push(arr.slice(i, i + size));
  }
  return chunks;
}
