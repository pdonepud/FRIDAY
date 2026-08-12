console.log("[FRIDAY UI] Frontend loaded.");

// ========== CONFIG ==========
const API_BASE = "http://127.0.0.1:8765";
const REFRESH_INTERVAL_MS = 5 * 60 * 1000;

// ========== BOOT SEQUENCE ==========
const STATUS_MESSAGES = [
  "INITIALIZING SYSTEMS",
  "ESTABLISHING SECURE CONNECTION",
  "ONLINE — WELCOME, PREETAM",
];

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

function startBootSequence() {
  setTimeout(() => showStatusMessage(STATUS_MESSAGES[0]), 1500);
  setTimeout(() => showStatusMessage(STATUS_MESSAGES[1]), 2200);
  setTimeout(() => showStatusMessage(STATUS_MESSAGES[2]), 2900);
  setTimeout(() => fadeOutBoot(), 3700);
}

// ========== FETCH HELPERS ==========
async function fetchJSON(path) {
  try {
    const r = await fetch(`${API_BASE}${path}`, {
      method: "GET",
      headers: { "Accept": "application/json" },
    });
    if (!r.ok) {
      console.error(`[FRIDAY UI] ${path} returned ${r.status}`);
      return null;
    }
    return await r.json();
  } catch (err) {
    console.error(`[FRIDAY UI] ${path} failed:`, err);
    return null;
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

  body.innerHTML = `
    <div class="weather-current">
      <span class="weather-temp">${formatTemp(data.temp_f)}</span>
      <span class="weather-unit">°F</span>
    </div>
    <div class="weather-conditions">${data.conditions}</div>
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

// ========== ORCHESTRATOR ==========
async function loadDashboard() {
  console.log("[FRIDAY UI] Loading dashboard...");
  await Promise.all([renderWeatherPanel(), renderCalendarPanel()]);
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
  startBootSequence();
  startAutoRefresh();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
