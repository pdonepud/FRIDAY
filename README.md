# FRIDAY

### A personal AI desktop assistant inspired by JARVIS.

Voice-first. Context-aware. Actually useful.

---

## Overview

**FRIDAY** is a Python-based personal AI assistant that lives on your desktop. It greets you by voice, reads your calendar, tracks your stocks, summarizes the news, monitors your sleep, and uses **Google Gemini** to deliver smart, context-aware briefings throughout your day.

It's built for people who want a real productivity co-pilot — not a chatbot in a browser tab.

```
"You have a meeting in 30 minutes and it's raining outside — you should leave early."
```

---

## Features

### Core Intelligence
- **Voice Greeting** — Randomized funny line on every launch via edge-tts
- **Daily Briefing** — 60-second Gemini-powered audio summary of your calendar, news, and stocks every morning
- **Context-Aware Reminders** — Combines calendar + weather to tell you when to leave
- **Conversational Q&A** — Hotkey + ask anything + Gemini answers by voice
- **"What should I do right now?"** — Feeds calendar gaps, news, stocks, weather, and sleep into Gemini for the best next-hour action

### Productivity
- **Focus Mode** — Blocks distracting sites, plays lo-fi, runs a Pomodoro timer, silences notifications for 25 minutes
- **Meeting Prep Cards** — Pop up 15 min before events with attendees, agenda, and Gemini-generated talking points
- **Clipboard AI** — Copy any text, hit a hotkey, get Gemini to summarize/translate/rewrite it aloud
- **Away Mode** — Detects 10 min of no input and mutes everything

### Markets & News
- **Pre-Market Alerts** at 9:15 AM — Voice-reads watchlist movers, correlates with morning news
- **News Sentiment Analysis** — Gemini reads top headlines and gives you the market mood
- **Portfolio Mood Ring** — Your stocks visualized as a single color with a one-line verdict

### Health & Wellbeing
- **Sleep Score Graphs** — Gemini spots patterns in your sleep data over time
- **Hydration & Posture Nudges** — Snarky personalized toast notifications every 45 min
- **Mood Tracker** — Daily rating correlated with sleep data

### Ambience
- **Weather-Reactive Music** — Playlists switch based on local conditions
- **End-of-Day Wrap** at 6 PM — Recaps meetings, news, stocks, and goals

---

## Demo

> *Demo GIF / screenshot coming once the Tauri UI is wired to live data (Phase 13).*

---

## Tech Stack

| Layer | Technology | Notes |
|------|------|------|
| Language | Python 3.11+ | |
| AI / LLM | Google Gemini API | `gemini-2.5-flash-lite` via REST (no SDK) |
| Voice Synthesis | edge-tts (Microsoft Neural Voices) | Free, no API key |
| Audio Playback | pygame | |
| HTTP API | FastAPI + uvicorn | Exposes the Python modules to the Tauri UI |
| UI | Tauri + Vanilla JS/HTML/CSS | Native desktop shell with web frontend — enables animations and rich visuals tkinter can't do |
| Notifications | plyer | |
| Global Hotkeys | keyboard | |
| Wake Word *(optional)* | pvporcupine + pyaudio | |
| Calendar | Google Calendar API | Read-only OAuth scope |
| News | NewsAPI | Free tier, source whitelisting via domains filter |
| Stocks | yfinance (Yahoo Finance) | No API key required, no daily rate limit, real-time data during market hours |
| Weather | Open-Meteo | No API key required |

---

## Quick Start

### Prerequisites

- **Python 3.11 or newer** — [python.org](https://www.python.org/downloads/)
- **Windows 10 / 11** (Linux/macOS support planned)
- A free **Google Gemini API key** — [aistudio.google.com](https://aistudio.google.com)

### Installation

```bash
# 1. Clone the repository
git clone https://github.com/<your-username>/FRIDAY.git
cd FRIDAY

# 2. Create a virtual environment (recommended)
python -m venv venv
venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up your config file
# Windows:
Copy-Item config.example.py config.py
# Mac/Linux:
# cp config.example.py config.py
# 5. Open config.py and fill in your API keys

# 6. Launch
python friday.py
```

That's it. FRIDAY will greet you out loud.

---

## Configuration

All settings live in `config.py`:

```python
# API Keys
GEMINI_API_KEY   = "your-gemini-key"
NEWSAPI_KEY      = "your-newsapi-key"
# Stocks now use yfinance — no API key required (Phase 5.8)

# Watchlist
STOCK_TICKERS = ["AAPL", "TSLA", "NVDA", "MSFT"]
NEWS_TOPICS   = ["technology", "AI", "markets"]

# Voice — any edge-tts voice works
VOICE = "en-US-JennyNeural"
```

### API Keys — Where to Get Them

| Service | URL | Cost |
|--------|-----|------|
| Google Gemini | [aistudio.google.com](https://aistudio.google.com) | Free, no card required |
| NewsAPI | [newsapi.org](https://newsapi.org) | Free tier |
| Google Calendar | [console.cloud.google.com](https://console.cloud.google.com) | Free (OAuth, not API key) |
| Porcupine *(optional)* | [console.picovoice.ai](https://console.picovoice.ai) | Free, for voice wake word |

> Stocks (yfinance) and Weather (Open-Meteo) require no API key.

> Without Porcupine, FRIDAY uses `Ctrl+Alt+F` as the wake trigger instead of voice activation.

---

## Architecture

```
FRIDAY/
├── friday.py                  # Main entry point
├── config.py                  # API keys & personal settings
├── requirements.txt
│
├── modules/                   # Pure-Python backend, each module testable standalone
│   ├── voice.py               # edge-tts wrapper (+ interruptible playback)
│   ├── greeting.py            # Randomized startup lines
│   ├── gemini.py              # Gemini API wrapper (REST, no SDK)
│   ├── calendar_api.py        # Google Calendar integration (read-only)
│   ├── news.py                # NewsAPI integration with source whitelisting
│   ├── stocks.py              # Watchlist quotes via yfinance
│   ├── weather.py             # Open-Meteo integration
│   ├── briefing.py            # Daily briefing orchestrator (Gemini)
│   ├── reminders.py           # Context-aware reminders
│   ├── focus_mode.py          # Pomodoro + site blocker
│   ├── clipboard_ai.py        # Clipboard AI handler
│   ├── sleep_log.py           # Sleep logging & analysis
│   ├── mood.py                # Mood tracker
│   ├── nudges.py              # Hydration/posture nudges
│   ├── music.py               # Weather-reactive music
│   ├── hotkeys.py             # Global hotkey handlers
│   └── dashboard.py           # tkinter UI — deprecated, replaced by Phase 13
│                              #   (retained in git history at commit 690920c)
│
├── server/                    # FastAPI HTTP layer exposing modules to the Tauri UI
│   └── api.py
│
├── ui/                        # Tauri shell + web frontend
│   ├── src/                   # HTML/CSS/JS (the UI itself)
│   └── src-tauri/             # Tauri/Rust shell (auto-generated)
│
├── data/                      # JSON state (sleep, mood, history)
├── music/                     # Your .mp3 files
│   ├── lofi/
│   ├── rainy/
│   └── sunny/
└── assets/
```

### Three-layer split

FRIDAY is split into three layers, each independently runnable:

1. **Backend modules (`modules/`)** — pure Python: gemini, calendar, news, stocks, weather, briefing, voice. Each runs standalone with `python modules/<name>.py`.
2. **HTTP API (`server/api.py`)** — FastAPI server exposing the modules as REST endpoints on `127.0.0.1:8765`.
3. **Desktop UI (`ui/`)** — Tauri shell (Rust + WebView) rendering an HTML/CSS/JS frontend that fetches from the API. Replaces the deprecated tkinter dashboard.

### Design Principles

1. **Modular** — Every module runs standalone for easy testing (`python modules/weather.py`)
2. **Voice-first** — Default output is spoken; UI is secondary
3. **Local-first** — Sleep, mood, and history stored locally in JSON
4. **Gemini-as-brain** — All reasoning happens in one place: `modules/gemini.py`

---

## Usage

### Daily Commands

```bash
# Normal launch — voice greeting
python friday.py

# Force-run daily briefing immediately (interruptible with ESC)
python friday.py --now

# Ask anything
python friday.py --ask "What's the weather like?"

# Today's calendar events
python friday.py --calendar

# Next upcoming event
python friday.py --next

# Log last night's sleep
python modules/sleep_log.py log
```

### Hotkeys

| Keys | Action |
|------|--------|
| `Ctrl+Alt+F` | Wake FRIDAY — ask a question |
| `Ctrl+Alt+C` | Clipboard AI — summarize/translate/rewrite |
| `Ctrl+Alt+N` | "What should I do right now?" |
| `Ctrl+Alt+M` | Toggle Focus Mode |
| `Ctrl+Alt+Q` | Quit FRIDAY |

---

## Roadmap

Built in testable phases. Each phase runs on its own before the next is stacked.

- [x] **Phase 1** — Project skeleton + config + voice greeting
- [x] **Phase 2** — Gemini wrapper + basic Q&A
- [x] **Phase 3** — Weather, stocks, and news modules
- [x] **Phase 4** — Google Calendar integration
- [x] **Phase 5** — Daily briefing (ties it all together)
- [x] **Phase 5.8** — Stocks moved to yfinance (no API key, no daily cap)
- [x] **Phase 6** — tkinter dashboard UI *(deprecated — replaced by Phase 13)*
- [ ] **Phase 7** — Global hotkeys + clipboard AI
- [ ] **Phase 8** — Focus mode + Pomodoro
- [ ] **Phase 9** — Sleep + mood tracker
- [ ] **Phase 10** — Nudges + away mode + weather-reactive music
- [ ] **Phase 11** — Meeting prep cards + end-of-day wrap
- [ ] **Phase 12** — Polish + optional voice wake word
- [ ] **Phase 13** — Tauri UI rewrite *(in progress)*
  - [x] 13.1 — Scaffold Tauri project, JARVIS boot screen renders
  - [ ] 13.2 — FastAPI server exposing backend modules
  - [ ] 13.3–13.10 — Wire data panels, animations, JARVIS visuals

---

## Development

Setup for contributors working on FRIDAY's Tier 1+ code (the `agent/` package, tests, CI).

```bash
# 1. Install FRIDAY plus dev tooling in editable mode.
#    Runtime deps come from agent/requirements.txt via pyproject's
#    dynamic dependencies; the [dev] extra adds pytest, coverage, ruff, pre-commit.
pip install -e ".[dev]"

# 2. Install pre-commit hooks (once per clone). Ruff (check + format) and
#    trailing-whitespace / end-of-file fixes then run automatically on every commit.
pre-commit install

# 3. Run the test suite.
pytest

# 4. Run with coverage (mirrors CI; floor is 60%).
pytest --cov=agent --cov-report=term-missing
```

GitHub Actions runs the same `lint` (ruff check + format check) and `test` (pytest + coverage) jobs on every push and pull request against `main`.

---

## Contributing

This is a personal project, but PRs and ideas are welcome. If you're building something similar or want to fork it for your own assistant:

1. Fork the repo
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Commit changes: `git commit -m 'Add something cool'`
4. Push: `git push origin feature/your-feature`
5. Open a Pull Request

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Author

**Preetam**

Building the assistant I wished existed.
