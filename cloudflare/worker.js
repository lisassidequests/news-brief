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
      await handleBrief(message, env, false);
      break;
    case "/preview":
      await handleBrief(message, env, true);
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
      await sendMessage(chatId, "Unknown command. Try /start, /brief, /settings, /pause, or /resume.", env);
  }
}

// =============================================================================
// /start — multi-step onboarding
// =============================================================================

async function handleStart(message, env) {
  const chatId = message.chat.id;
  const telegramId = message.from.id;

  // Check if user already exists
  const existing = await getUser(telegramId, env);
  if (existing && !existing.onboarding_step) {
    await sendMessage(
      chatId,
      `Welcome back, *${existing.name}*! 👋\n\nYour brief is scheduled for ${existing.delivery_time} SGT.\n\nUse /settings to change preferences or /brief for an instant brief.`,
      env
    );
    return;
  }

  // Start fresh onboarding — step 1: ask for name
  await upsertUser(
    {
      telegram_id: telegramId,
      name: message.from.first_name || "Friend",
      onboarding_step: "awaiting_name",
      is_active: true,
      is_admin: false,
    },
    env
  );

  await sendMessage(
    chatId,
    `👋 Welcome to the *Cyber Intel Brief Bot*!\n\nI deliver a daily AI-generated intelligence brief on cybersecurity, AI governance, and supply chain threats.\n\nFirst — what should I call you? (Reply with your preferred name)`,
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
  if (!user || !user.onboarding_step) {
    // Not in onboarding — ignore or prompt
    await sendMessage(chatId, "Use /start to set up your brief or /brief to get one now.", env);
    return;
  }

  switch (user.onboarding_step) {
    case "awaiting_name": {
      const name = text.trim().slice(0, 50); // cap length
      await upsertUser({ telegram_id: telegramId, name, onboarding_step: "awaiting_format" }, env);
      // Ask for format via inline keyboard
      await sendMessageWithKeyboard(
        chatId,
        `Great, ${name}! 📋\n\nHow would you like to receive your brief?`,
        [
          [
            { text: "📄 Full Brief", callback_data: "fmt:full" },
            { text: "⚡ TL;DR", callback_data: "fmt:tldr" },
            { text: "🔗 Links Only", callback_data: "fmt:links" },
          ],
        ],
        env
      );
      break;
    }

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
          `Use /brief anytime for an instant brief, or /settings to change your preferences.`,
        env
      );
      break;
    }

    case "awaiting_settings_field": {
      // User is changing a setting conversationally
      await handleSettingsUpdate(message, user, env);
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

  // Always acknowledge the callback to stop Telegram's loading spinner
  await answerCallbackQuery(callbackQuery.id, env);

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
}

// =============================================================================
// /settings — show current settings and allow changes
// =============================================================================

async function handleSettings(message, env) {
  const chatId = message.chat.id;
  const telegramId = message.from.id;

  const user = await getUser(telegramId, env);
  if (!user) {
    await sendMessage(chatId, "No account found. Run /start to set up your brief.", env);
    return;
  }

  const topics = Array.isArray(user.topics) ? user.topics : JSON.parse(user.topics || "[]");
  const statusEmoji = user.is_active ? "✅ Active" : "⏸ Paused";

  await sendMessage(
    chatId,
    `⚙️ *Your Current Settings*\n\n` +
      `👤 Name: ${user.name}\n` +
      `📋 Format: ${user.format.toUpperCase()}\n` +
      `🏷 Topics: ${topics.length ? topics.join(", ") : "All"}\n` +
      `🕗 Delivery: 8:00 AM SGT daily\n` +
      `📊 Status: ${statusEmoji}\n` +
      `📝 Preferences: ${user.preferences || "None"}\n\n` +
      `To change a setting, reply with what you'd like to update.\n` +
      `_Example: "Change my format to TL;DR" or "Update topics to ransomware, AI"_`,
    env
  );

  // Put user into settings-update mode
  await upsertUser({ telegram_id: telegramId, onboarding_step: "awaiting_settings_field" }, env);
}

async function handleSettingsUpdate(message, user, env) {
  const chatId = message.chat.id;
  const telegramId = message.from.id;
  const text = (message.text || "").toLowerCase();

  let updated = false;

  if (text.includes("format") || text.includes("brief")) {
    const fmt = text.includes("tldr") || text.includes("tl;dr")
      ? "tldr"
      : text.includes("links") || text.includes("link")
      ? "links"
      : "full";
    await upsertUser({ telegram_id: telegramId, format: fmt, onboarding_step: null }, env);
    await sendMessage(chatId, `✅ Format updated to *${fmt.toUpperCase()}*.`, env);
    updated = true;
  } else if (text.includes("topic") || text.includes("interest")) {
    // Extract everything after the keyword as topics
    await upsertUser({ telegram_id: telegramId, onboarding_step: "awaiting_topics" }, env);
    await sendMessage(chatId, "What topics would you like? Reply with a comma-separated list.", env);
    updated = true;
  } else if (text.includes("name")) {
    await upsertUser({ telegram_id: telegramId, onboarding_step: "awaiting_name" }, env);
    await sendMessage(chatId, "What name should I use for you?", env);
    updated = true;
  }

  if (!updated) {
    await upsertUser({ telegram_id: telegramId, onboarding_step: null }, env);
    await sendMessage(
      chatId,
      "I didn't catch that. You can change your *format*, *topics*, or *name*. Use /settings to try again.",
      env
    );
  }
}

// =============================================================================
// /brief and /preview — trigger GitHub Actions workflow_dispatch
// =============================================================================

async function handleBrief(message, env, isPreview) {
  const chatId = message.chat.id;
  const telegramId = message.from.id;

  const user = await getUser(telegramId, env);
  if (!user) {
    await sendMessage(chatId, "Run /start first to set up your account.", env);
    return;
  }

  const prefix = isPreview
    ? "⚡ *Preview* — your scheduled brief still sends at 8am SGT.\n\n"
    : "";

  await sendMessage(
    chatId,
    `${prefix}⏳ Generating your brief now… This takes about 30-60 seconds.`,
    env
  );

  // Trigger the manual_brief workflow on GitHub Actions
  const triggered = await triggerGitHubWorkflow(telegramId, env);
  if (!triggered) {
    await sendMessage(
      chatId,
      "❌ Failed to trigger brief generation. Please try again in a minute.",
      env
    );
  }
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

async function triggerGitHubWorkflow(telegramId, env) {
  const url = `https://api.github.com/repos/${env.GITHUB_REPO_OWNER}/${env.GITHUB_REPO_NAME}/actions/workflows/manual_brief.yml/dispatches`;

  const body = {
    ref: env.GITHUB_REF || "main",
    inputs: { telegram_id: String(telegramId) },
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
    return resp.status === 204;
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
  const headers = {
    apikey: env.SUPABASE_SERVICE_KEY,
    Authorization: `Bearer ${env.SUPABASE_SERVICE_KEY}`,
    "Content-Type": "application/json",
    // Return the full object on insert/upsert
    Prefer: "return=representation",
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
