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

> *Demo GIF / screenshot coming once the dashboard ships in Phase 6.*

---

## Tech Stack

| Layer | Technology |
|------|------|
| Language | Python 3.11+ |
| AI / LLM | Google Gemini API |
| Voice Synthesis | edge-tts (Microsoft Neural Voices) |
| Audio Playback | pygame |
| UI | tkinter |
| Notifications | plyer |
| Global Hotkeys | keyboard |
| Wake Word *(optional)* | pvporcupine + pyaudio |
| Calendar | Google Calendar API |
| News | NewsAPI |
| Stocks | Alpha Vantage |
| Weather | Open-Meteo (no key required) |

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

# 4. Configure your keys
# Open config.py and fill in your API keys and personal info

# 5. Launch
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
ALPHAVANTAGE_KEY = "your-av-key"

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
| Alpha Vantage | [alphavantage.co](https://alphavantage.co) | Free tier |
| Google Calendar | [console.cloud.google.com](https://console.cloud.google.com) | Free (OAuth, not API key) |
| Porcupine *(optional)* | [console.picovoice.ai](https://console.picovoice.ai) | Free, for voice wake word |

> Without Porcupine, FRIDAY uses `Ctrl+Alt+F` as the wake trigger instead of voice activation.

---

## Architecture

```
FRIDAY/
├── friday.py                  # Main entry point
├── config.py                  # API keys & personal settings
├── requirements.txt
│
├── modules/
│   ├── voice.py               # edge-tts wrapper
│   ├── greeting.py            # Randomized startup lines
│   ├── gemini.py              # Gemini API wrapper
│   ├── calendar_api.py        # Google Calendar integration
│   ├── news.py                # NewsAPI integration
│   ├── stocks.py              # Alpha Vantage integration
│   ├── weather.py             # Open-Meteo integration
│   ├── briefing.py            # Daily briefing orchestrator
│   ├── reminders.py           # Context-aware reminders
│   ├── focus_mode.py          # Pomodoro + site blocker
│   ├── clipboard_ai.py        # Clipboard AI handler
│   ├── sleep_log.py           # Sleep logging & analysis
│   ├── mood.py                # Mood tracker
│   ├── nudges.py              # Hydration/posture nudges
│   ├── music.py               # Weather-reactive music
│   ├── hotkeys.py             # Global hotkey handlers
│   └── dashboard.py           # tkinter UI
│
├── data/                      # JSON state (sleep, mood, history)
├── music/                     # Your .mp3 files
│   ├── lofi/
│   ├── rainy/
│   └── sunny/
└── assets/
```

### Design Principles

1. **Modular** — Every module runs standalone for easy testing (`python modules/weather.py`)
2. **Voice-first** — Default output is spoken; UI is secondary
3. **Local-first** — Sleep, mood, and history stored locally in JSON
4. **Gemini-as-brain** — All reasoning happens in one place: `modules/gemini.py`

---

## Usage

### Daily Commands

```bash
# Normal launch — starts scheduler, greeting, dashboard
python friday.py

# Force-run daily briefing immediately (for testing)
python friday.py --now

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
- [ ] **Phase 2** — Gemini wrapper + basic Q&A
- [ ] **Phase 3** — Weather, stocks, and news modules
- [ ] **Phase 4** — Google Calendar integration
- [ ] **Phase 5** — Daily briefing (ties it all together)
- [ ] **Phase 6** — tkinter dashboard UI
- [ ] **Phase 7** — Global hotkeys + clipboard AI
- [ ] **Phase 8** — Focus mode + Pomodoro
- [ ] **Phase 9** — Sleep + mood tracker
- [ ] **Phase 10** — Nudges + away mode + weather-reactive music
- [ ] **Phase 11** — Meeting prep cards + end-of-day wrap
- [ ] **Phase 12** — Polish + optional voice wake word

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
