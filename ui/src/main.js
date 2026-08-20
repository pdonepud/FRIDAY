console.log("[FRIDAY UI] Frontend loaded.");

// ========== CONFIG ==========
const API_BASE = "http://127.0.0.1:8765";
const REFRESH_INTERVAL_MS = 5 * 60 * 1000;

// ========== BOOT SEQUENCE ==========
const STATUS_MESSAGES = {
  init:           "INITIALIZING SYSTEMS",
  connecting:     "ESTABLISHING SECURE CONNECTION",
  starting:       "STARTING BACKEND SERVICES",
  online:         "ONLINE — WELCOME, PREETAM",
  serverDown:     "SERVER NOT RESPONDING — CHECK CONSOLE",
};

// Health-gate tuning. The Rust sidecar launches uvicorn on window open;
// uvicorn typically listens within ~1-3s but a cold Python import can take
// longer, so we allow up to 20s. Poll cadence stays snappy so the boot
// screen doesn't linger once the server is actually up.
const HEALTH_POLL_INTERVAL_MS = 500;
const HEALTH_POLL_MAX_MS = 20_000;
const HEALTH_REQUEST_TIMEOUT_MS = 2_000;

const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

function showStatusMessage(text) {
  const el = document.getElementById("boot-status");
  if (!el) return;
  el.classList.remove("visible");
  void el.offsetWidth;
  el.textContent = text;
  el.classList.add("visible");
}

function fadeOutBoot() {
  const boot = document.getElementById("boot");
  const app = document.getElementById("app");
  if (!boot || !app) return;
  boot.classList.add("boot-fading");
  app.classList.remove("app-hidden");
  app.classList.add("app-visible");
  setTimeout(() => {
    boot.classList.add("boot-hidden");
    loadDashboard();
  }, 600);
}

// Polls /api/health until it responds 200 or the deadline passes.
// Returns true if the server came up, false on timeout.
async function waitForServer() {
  const deadline = Date.now() + HEALTH_POLL_MAX_MS;
  let attempts = 0;
  while (Date.now() < deadline) {
    attempts++;
    const remainingMs = deadline - Date.now();
    if (remainingMs <= 0) break;
    const controller = new AbortController();
    const timeoutId = setTimeout(
      () => controller.abort(),
      Math.min(HEALTH_REQUEST_TIMEOUT_MS, remainingMs),
    );
    try {
      const r = await fetch(`${API_BASE}/api/health`, { signal: controller.signal });
      if (r.ok) {
        console.log(`[FRIDAY UI] Server reachable after ${attempts} attempt(s)`);
        return true;
      }
    } catch {
      // Connection refused / abort — retry until deadline.
    } finally {
      clearTimeout(timeoutId);
    }
    // Only sleep if the next attempt would still fit inside the deadline —
    // otherwise the loop exits anyway and the sleep is wasted, pushing the
    // observed timeout past HEALTH_POLL_MAX_MS.
    if (Date.now() + HEALTH_POLL_INTERVAL_MS < deadline) {
      await sleep(HEALTH_POLL_INTERVAL_MS);
    } else {
      break;
    }
  }
  console.error(`[FRIDAY UI] Server did not respond within ${HEALTH_POLL_MAX_MS}ms (${attempts} attempts)`);
  return false;
}

async function startBootSequence() {
  // First two messages are pure boot atmosphere on fixed timers.
  await sleep(1500); showStatusMessage(STATUS_MESSAGES.init);
  await sleep(700);  showStatusMessage(STATUS_MESSAGES.connecting);
  await sleep(700);  showStatusMessage(STATUS_MESSAGES.starting);

  // Now actually wait for the FastAPI server. This gates the dashboard
  // render so panels don't fire fetches into a dead port and flash OFFLINE.
  const ready = await waitForServer();
  if (!ready) {
    // Leave the boot screen up with a persistent error — dashboard cannot
    // render without a server, and blank panels would be worse than an
    // explicit failure state.
    showStatusMessage(STATUS_MESSAGES.serverDown);
    return;
  }

  showStatusMessage(STATUS_MESSAGES.online);
  await sleep(800);
  fadeOutBoot();
}

// ========== FETCH HELPERS ==========
async function fetchJSON(path) {
  // 30-second timeout — prevents dashboard from freezing on a hung server.
  // Without this, auto-refresh (every 5 min) can pile up pending requests indefinitely.
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 30_000);

  try {
    const r = await fetch(`${API_BASE}${path}`, {
      method: "GET",
      headers: { "Accept": "application/json" },
      signal: controller.signal,
    });
    if (!r.ok) {
      console.error(`[FRIDAY UI] ${path} returned ${r.status}`);
      return null;
    }
    return await r.json();
  } catch (err) {
    if (err.name === "AbortError") {
      console.error(`[FRIDAY UI] ${path} timed out after 30s`);
    } else {
      console.error(`[FRIDAY UI] ${path} failed:`, err);
    }
    return null;
  } finally {
    clearTimeout(timeoutId);
  }
}

function setPanelStatus(id, text, isError = false) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("error", isError);
}

// ========== WEATHER ==========
function formatTemp(f) { return Math.round(f); }

async function renderWeatherPanel() {
  const body = document.getElementById("weather-body");
  if (!body) return;
  setPanelStatus("weather-status", "FETCHING");

  const data = await fetchJSON("/api/weather");
  if (!data) {
    body.innerHTML = `<div class="panel-error">
      Could not reach the weather service.<br>
      Is the FastAPI server running? <code>python -m server.api</code>
    </div>`;
    setPanelStatus("weather-status", "OFFLINE", true);
    return;
  }

  const conditions = escapeHtml(data.conditions || "Unknown");

  body.innerHTML = `
    <div class="weather-current">
      <span class="weather-temp">${formatTemp(data.temp_f)}</span>
      <span class="weather-unit">°F</span>
    </div>
    <div class="weather-conditions">${conditions}</div>
    <div class="weather-meta">
      <div class="weather-meta-row"><span>Feels like</span><span class="weather-meta-value">${formatTemp(data.feels_like_f)}°</span></div>
      <div class="weather-meta-row"><span>Today's high/low</span><span class="weather-meta-value">${formatTemp(data.today_high_f)}° / ${formatTemp(data.today_low_f)}°</span></div>
      <div class="weather-meta-row"><span>Rain chance</span><span class="weather-meta-value">${data.rain_chance_today}%</span></div>
    </div>
  `;
  setPanelStatus("weather-status", "LIVE");
}

// ========== CALENDAR ==========
function formatEventTime(isoString, isAllDay) {
  if (isAllDay) return "ALL DAY";
  try {
    const date = new Date(isoString);
    return date.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true });
  } catch {
    return "";
  }
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

async function renderCalendarPanel() {
  const body = document.getElementById("calendar-body");
  if (!body) return;
  setPanelStatus("calendar-status", "FETCHING");

  const events = await fetchJSON("/api/calendar/today");
  if (events === null) {
    body.innerHTML = `<div class="panel-error">
      Could not reach the calendar service.<br>
      Is the FastAPI server running? <code>python -m server.api</code>
    </div>`;
    setPanelStatus("calendar-status", "OFFLINE", true);
    return;
  }

  if (events.length === 0) {
    body.innerHTML = `<div class="panel-empty">Nothing scheduled today.</div>`;
    setPanelStatus("calendar-status", "EMPTY");
    return;
  }

  const rowsHtml = events.map(event => {
    const timeStr = formatEventTime(event.start, event.all_day);
    const titleStr = escapeHtml(event.title || "(no title)");
    const allDayClass = event.all_day ? "all-day" : "";
    return `
      <div class="calendar-event ${allDayClass}">
        <div class="calendar-event-time">${timeStr}</div>
        <div class="calendar-event-title">${titleStr}</div>
      </div>
    `;
  }).join("");

  body.innerHTML = `<div class="calendar-list">${rowsHtml}</div>`;
  setPanelStatus("calendar-status", `${events.length} EVENTS`);
}

// ========== WATCHLIST ==========

// Mirror modules/stocks.py's _FLAT_THRESHOLD_PCT exactly — keeps the UI's
// gray "flat" coloring aligned with what the spoken briefing calls flat.
const WATCHLIST_FLAT_THRESHOLD_PCT = 0.2;

function formatPrice(n) {
  return `$${Number(n).toFixed(2)}`;
}

function formatChange(n) {
  const sign = n >= 0 ? "+" : "";
  return `${sign}${Number(n).toFixed(2)}`;
}

function formatChangePct(n) {
  const sign = n >= 0 ? "+" : "";
  return `${sign}${Number(n).toFixed(2)}%`;
}

function changeDirection(changePct) {
  if (changePct > WATCHLIST_FLAT_THRESHOLD_PCT) return "up";
  if (changePct < -WATCHLIST_FLAT_THRESHOLD_PCT) return "down";
  return "flat";
}

async function renderWatchlistPanel() {
  const body = document.getElementById("watchlist-body");
  if (!body) return;
  setPanelStatus("watchlist-status", "FETCHING");

  const quotes = await fetchJSON("/api/stocks");
  if (quotes === null) {
    body.innerHTML = `<div class="panel-error">
      Could not reach the stocks service.<br>
      Is the FastAPI server running? <code>python -m server.api</code>
    </div>`;
    setPanelStatus("watchlist-status", "OFFLINE", true);
    return;
  }

  if (quotes.length === 0) {
    body.innerHTML = `<div class="panel-empty">No tickers configured.</div>`;
    setPanelStatus("watchlist-status", "EMPTY");
    return;
  }

  const rowsHtml = quotes.map(q => {
    const ticker = escapeHtml(q.ticker || "");
    const direction = changeDirection(q.change_pct);
    const staleClass = q.stale ? "stale" : "";
    // Stale rows get a visible "CACHED" label directly in the DOM so that
    // keyboard-only and screen-reader users get the same signal as sighted
    // users. Title tooltip is kept as supplementary info for hover.
    const staleLabel = q.stale
      ? ' <span class="watchlist-stale-label">CACHED</span>'
      : "";
    const titleAttr = q.stale
      ? ' title="Cached price — live fetch unavailable"'
      : "";
    return `
      <div class="watchlist-row ${staleClass}"${titleAttr}>
        <div class="watchlist-ticker">${ticker}${staleLabel}</div>
        <div class="watchlist-price">${formatPrice(q.price)}</div>
        <div class="watchlist-change ${direction}">${formatChange(q.change)} (${formatChangePct(q.change_pct)})</div>
      </div>
    `;
  }).join("");

  body.innerHTML = `<div class="watchlist-list">${rowsHtml}</div>`;
  setPanelStatus("watchlist-status", `LIVE · ${quotes.length} TICKERS`);
}

// ========== NEWS ==========

// Matches the /api/news endpoint's own default quotas (server/api.py:242).
// Kept in sync manually — no shared config file between Python and JS today.
const NEWS_QUOTAS = { politics: 2, world: 1, markets: 1, tech: 1 };

// Prefer the Tauri shell plugin (opens URLs in the system default browser
// so headlines don't try to render inside the webview). Falls back to
// window.open for dev-in-plain-browser scenarios where Tauri isn't present.
async function openExternalUrl(url) {
  if (window.__TAURI__ && window.__TAURI__.shell) {
    try {
      await window.__TAURI__.shell.open(url);
      return;
    } catch (err) {
      console.error("[FRIDAY UI] Tauri shell.open failed, falling back:", err);
    }
  }
  window.open(url, "_blank", "noopener,noreferrer");
}

function buildNewsQueryString(quotas) {
  const params = new URLSearchParams();
  for (const [key, val] of Object.entries(quotas)) {
    params.set(key, String(val));
  }
  return params.toString();
}

async function renderNewsPanel() {
  const body = document.getElementById("news-body");
  if (!body) return;
  setPanelStatus("news-status", "FETCHING");

  const qs = buildNewsQueryString(NEWS_QUOTAS);
  const articles = await fetchJSON(`/api/news?${qs}`);
  if (articles === null) {
    body.innerHTML = `<div class="panel-error">
      Could not reach the news service.<br>
      Is the FastAPI server running? <code>python -m server.api</code>
    </div>`;
    setPanelStatus("news-status", "OFFLINE", true);
    return;
  }

  if (articles.length === 0) {
    body.innerHTML = `<div class="panel-empty">No headlines available right now.</div>`;
    setPanelStatus("news-status", "EMPTY");
    return;
  }

  const rowsHtml = articles.map(article => {
    const title = escapeHtml(article.title || "(no title)");
    const source = escapeHtml(article.source || "");
    const category = escapeHtml(article.category || "");
    const url = escapeHtml(article.url || "");
    const staleClass = article.stale ? "stale" : "";
    // Category class is derived from the category string; unknown categories
    // fall through to .news-category's default muted gray, so a new
    // category from the backend won't crash the page — it just won't get a
    // dedicated color until we add one.
    const categoryClass = `news-category-${category}`;
    // Stale rows get a visible "CACHED" label inside the source cell so
    // keyboard/screen-reader users get the same signal as sighted users —
    // same fix pattern as .watchlist-stale-label on PR #29. Tooltip on the
    // row is the supplementary hover explanation.
    const staleLabel = article.stale
      ? ' <span class="news-stale-label">CACHED</span>'
      : "";
    const titleAttr = article.stale
      ? ' title="Cached headline — live fetch unavailable"'
      : "";
    // data-url lets us delegate a single click listener on the container
    // instead of attaching one per row (also avoids inline onclick=""
    // injection concerns).
    return `
      <button type="button" class="news-row ${staleClass}" data-url="${url}"${titleAttr}>
        <span class="news-category ${categoryClass}">${category}</span>
        <span class="news-title">${title}</span>
        <span class="news-source">${source}${staleLabel}</span>
      </button>
    `;
  }).join("");

  body.innerHTML = `<div class="news-list">${rowsHtml}</div>`;

  // Single delegated click listener — cheaper than per-row and survives
  // re-renders on the 5-min refresh (the container is replaced each time,
  // so the old listener is garbage-collected).
  const list = body.querySelector(".news-list");
  if (list) {
    list.addEventListener("click", (event) => {
      const row = event.target.closest(".news-row");
      if (!row) return;
      const url = row.getAttribute("data-url");
      if (url) openExternalUrl(url);
    });
  }

  setPanelStatus("news-status", `LIVE · ${articles.length} HEADLINES`);
}

// ========== BRIEFING BUTTON ==========

let briefingPollTimer = null;
const BRIEFING_POLL_INITIAL_MS = 500;
const BRIEFING_POLL_LONG_MS = 2000;

function setBriefingButton(disabled, label = "Run Briefing") {
  const btn = document.getElementById("briefing-button");
  if (!btn) return;
  btn.disabled = disabled;
  btn.textContent = label;
}

function setBriefingMessage(text, isError = false) {
  const el = document.getElementById("briefing-message");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("error", isError);
}

function stopBriefingPolling() {
  if (briefingPollTimer !== null) {
    clearTimeout(briefingPollTimer);
    briefingPollTimer = null;
  }
}

async function pollBriefingStatus() {
  const status = await fetchJSON("/api/briefing/status");
  if (!status) {
    setPanelStatus("briefing-status", "OFFLINE", true);
    setBriefingMessage("Lost connection to server.", true);
    setBriefingButton(false);
    stopBriefingPolling();
    return;
  }

  const { status: s, message } = status;
  setPanelStatus("briefing-status", s.toUpperCase());
  setBriefingMessage(message, s === "error");

  if (s === "generating" || s === "speaking") {
    // Long-running phase — poll less aggressively
    briefingPollTimer = setTimeout(pollBriefingStatus, BRIEFING_POLL_LONG_MS);
  } else {
    // idle | done | stopped | error — stop polling, re-enable button
    stopBriefingPolling();
    setBriefingButton(false, "Run Briefing");
  }
}

async function handleBriefingClick() {
  setBriefingButton(true, "Starting...");
  setBriefingMessage("");
  setPanelStatus("briefing-status", "STARTING");

  try {
    const response = await fetch(`${API_BASE}/api/briefing/speak`, {
      method: "POST",
      headers: { "Accept": "application/json" },
    });
    if (!response.ok) {
      setBriefingMessage(`Server error: ${response.status}`, true);
      setPanelStatus("briefing-status", "ERROR", true);
      setBriefingButton(false);
      return;
    }
    const result = await response.json();
    if (!result.accepted) {
      // Already playing
      setBriefingMessage(result.message, true);
      setPanelStatus("briefing-status", "BUSY", true);
      setBriefingButton(false);
      return;
    }
    // Accepted — start polling
    setBriefingButton(true, "Running...");
    briefingPollTimer = setTimeout(pollBriefingStatus, BRIEFING_POLL_INITIAL_MS);
  } catch (err) {
    console.error("[FRIDAY UI] Briefing start failed:", err);
    setBriefingMessage("Could not reach the briefing service.", true);
    setPanelStatus("briefing-status", "OFFLINE", true);
    setBriefingButton(false);
  }
}

function initBriefingButton() {
  const btn = document.getElementById("briefing-button");
  if (!btn) return;
  btn.addEventListener("click", handleBriefingClick);
  setPanelStatus("briefing-status", "READY");
}

// ========== ORCHESTRATOR ==========
async function loadDashboard() {
  console.log("[FRIDAY UI] Loading dashboard...");
  await Promise.all([
    renderWeatherPanel(),
    renderCalendarPanel(),
    renderWatchlistPanel(),
    renderNewsPanel(),
  ]);
  console.log("[FRIDAY UI] Dashboard loaded.");
}

function startAutoRefresh() {
  setInterval(() => {
    console.log("[FRIDAY UI] Auto-refresh tick");
    loadDashboard();
  }, REFRESH_INTERVAL_MS);
}

// ========== ENTRY ==========
function init() {
  // startBootSequence() is async — an unhandled rejection here would freeze
  // the boot screen silently. Surface the failure into the same persistent
  // error state the health-gate timeout uses.
  startBootSequence().catch((err) => {
    console.error("[FRIDAY UI] Boot sequence failed:", err);
    showStatusMessage(STATUS_MESSAGES.serverDown);
  });
  startAutoRefresh();
  initBriefingButton();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
