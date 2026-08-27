# F.R.I.D.A.Y. — Voice-First Personal AI Companion

**Author:** Preetam Donepudi
**Written:** August 25, 2026 (Tier 0 interview)
**Status:** Active spec — this is the single source of truth for what
FRIDAY is and how it's built. Any future session (Preetam's, Claude
Chat's, Claude Code's) should read this first.

---

## What FRIDAY is

A voice-first personal AI companion that helps me stay on top of my
day at UCSC and my personal projects. Voice is the primary interface;
visual panels appear on demand and vanish when the conversation
resumes. It's ambient by default — the arcs are the resting state,
panels are what appears when I ask for them.

Not a dashboard app with voice bolted on. Not a chatbot with a UI.
An assistant I talk to that happens to also show me things when it
helps.

## Who it's for

Me (Preetam) only, right now. Future expansion to a small circle
(possibly roommates, study partners) is a "someday" not a "now" —
design choices don't need to accommodate it today, but per-user
state should not be actively prevented.

## Personality

Playful, British, gently dry. Think F.R.I.D.A.Y. from the Iron Man
films — knowledgeable, capable, warm but never sycophantic, willing
to add small commentary rather than being purely factual.

Concrete markers:
- Greets by name once per session ("Good morning, Preetam") — then
  gets straight into answers without re-greeting
- Uses contractions ("you're", "here's")
- Acknowledges before responding, doesn't just start answering
- Adds small commentary when natural ("Bit of a busy day, then")
- Never grovels, never over-apologizes, never adds filler

## First three capabilities (Tier 2 tools)

Ordered by how central they are to daily use:

1. **Canvas assignment lookup** — "What's due this week?" pulls from
   the UCSC Canvas API and reports back. First tool built.
2. **Stock research** — "What's happening with NVDA today?" pulls
   current quote, recent news, basic context. Reuses existing
   `modules/stocks.py` and `modules/news.py`. Read-only — no trade
   execution.
3. **Traffic-aware departure time** — "When should I leave for
   campus?" checks current traffic conditions and gives a departure
   time.

Deferred to later tiers:
- Assignment *reminders* — this is Tier 5 heartbeat work, not a Tier 2
  tool. Runs Canvas lookup on a schedule and surfaces due-soon items.
- Trade *execution* via Robinhood — huge safety surface area, deferred
  until baseline is rock-solid AND explicit safety-review sprint has
  happened.

## Architecture direction

### Now (Tier 2 onward)
Direct tools on a single FRIDAY agent. The three tools above live in
`agent/tools/` as self-contained functions, registered with the agent
via a tool registry.

### Target end-state (post-baseline)
Sub-agent architecture. Each domain gets its own specialist agent
with its own prompt and its own tool subset, reporting back to
FRIDAY-main. Planned split:

- **Finance sub-agent** — stock research, eventually trade execution.
  Hard-isolated; strictest safety rails; no other agent can call its
  tools.
- **Canvas sub-agent** — assignment lookup, deadline tracking, course
  info.
- **Traffic sub-agent** — departure planning, route lookups.
- **FRIDAY-main** — conversation, personality, delegation, memory.

**Why not sub-agents on day one:** sub-agents are architecturally
identical to a single agent, just with narrower scope. Building the
hand-off protocol against a moving target (before we know what the
main agent looks like) means tearing it down and rebuilding. The
document's discipline — "build the baseline, then extend" — applies
literally here.

## Stack

| Layer | Choice |
|-------|--------|
| Language | Python 3.11+ |
| Agent brain | Claude Sonnet 4.6 via official Anthropic Python SDK |
| Speech-to-text | Deepgram (Tier 3) |
| Text-to-speech | ElevenLabs, female British voice (Tier 3) |
| Runtime | Local laptop, Windows |

Every external provider (Claude, Deepgram, ElevenLabs) sits behind
a thin seam — a single small function that's the only place in the
codebase that talks to that provider's SDK. This is the swap-friendly
architecture from the design document.

All API keys live in environment variables or a git-ignored `.env`
file. Never in source, never in commits, never in logs.

## Interaction model

| Tier | How I talk to FRIDAY |
|------|----------------------|
| 1 | Text in terminal (REPL) |
| 2 | Text in terminal, now with working tools |
| 3 | **Push-to-talk on Right Alt.** Text still works. |
| Later | Wake word / always-listening — deferred |

Push-to-talk deliberately chosen over wake word for reliability:
sidesteps false triggers, self-listening loops, and privacy concerns.
Wake word is a "someday when everything else is solid" feature, not a
Tier 3 goal.

## Confirmation gate — never without asking

The following actions must ALWAYS pause for explicit confirmation
before running, regardless of who initiated them (me speaking,
heartbeat, tool chain). Confirmation states plainly what's about to
happen and waits for my "yes":

- Place any trade (buy/sell, any asset, any amount)
- Send any message on my behalf (email, text, DM, chat)
- Delete or overwrite any file or note
- Change any system setting
- Spend money in any form (subscriptions, purchases, API top-ups)
- Home automation actions (garage door, locks, appliances) — future

Convenience features that reduce friction without weakening the gate:
- **Batched confirmations** — multiple items in one voice command
  confirm together with a single "yes"
- **Silence-is-no default** — if I don't answer within 30 seconds,
  the action is *not* executed. Silence never means yes.
- **Audit trail** — every consequential action FRIDAY takes on my
  behalf is logged plainly, viewable at any time
- **Small pre-approved list** — a very short, explicit, config-file-
  managed list of things auto-approved under a dollar threshold
  (e.g., "top up ElevenLabs API under $10 is auto-OK"). Never
  includes trades. Never includes anything irreversible.

## Proactive behavior (Tier 5)

Yes, FRIDAY reaches out first — but quiet by default, per the design
document's Tier 5 guidance:

- Most scheduled checks produce nothing most of the time
- A calm log accumulates notes I can glance at when I choose
- Only genuinely noteworthy things earn an actual interruption
- Quiet hours are honored (non-urgent items wait for waking hours)
- Notices are held for me on return — never dropped into the void
  if I wasn't looking
- Every surfaced item is dismissible

## Runtime location

- Laptop-first for Tiers 1-5. Heartbeat beats only while laptop is
  awake — acceptable for now.
- Designed so the heartbeat *can* relocate to an always-on host later
  without rewriting the loop. Not something we solve today.

## Repository location

**Must move out of OneDrive before Tier 1 starts.** OneDrive sync
causes real cold-start latency issues that have burned time in prior
sprints. Target path: `C:\Projects\FRIDAY\` or similar non-synced
location. Backup via git remotes (GitHub), not OneDrive.

This is a Tier 0 prerequisite — done before writing a single line of
agent code.

## Future vision (documented, not built)

These are real product commitments, but they are explicitly out of
scope until baseline six tiers are complete AND their own dedicated
sprints have been planned with proper design work:

- **Home automation** — IoT integration, garage door, lights,
  appliances. Requires hard confirmation gate for every physical
  action.
- **Screen vision** — "see what I'm working on, tell me what to do
  next." Requires vision model integration, real privacy design.
- **Camera monitoring** — "watch what I'm doing, tell me if I'm
  doing it right." Requires camera consent flow, real privacy design.
- **Live trade execution via Robinhood** — sub-agent architecture,
  substantial safety review, gate is non-negotiable.

Each of these gets its own vision doc + sprint plan when its time
comes. Not before.

## Build discipline

This project follows the tier-by-tier structure of the source design
document:

- **Tier 0** — This document. Interview + spec. (Done.)
- **Tier 1** — Text-only conversation loop with streaming.
- **Tier 2** — Tool registry + first three tools.
- **Tier 3** — Voice (Deepgram in, ElevenLabs out), push-to-talk.
- **Tier 4** — Long-term memory across restarts.
- **Tier 5** — Heartbeat / proactive behavior.
- **Tier 6** — Confirmation gate, config, audit, kill switch.
- **Beyond** — Sub-agents, face (dashboard revival), always-on host,
  future-vision items.

Each tier ends with a verification step. Don't start a tier until the
previous one works on its own. Don't fuse tiers together. Text first,
always — voice is a layer on top.

## What happens to existing FRIDAY code

- `modules/weather.py`, `modules/news.py`, `modules/stocks.py`,
  `modules/briefing.py` — kept, become the raw material for Tier 2
  tools
- `server/api.py` — kept for now, may be retired if the agent calls
  modules directly
- `ui/` — dormant. The Tauri dashboard stays runnable but no new
  work happens there until it comes back to life as "the face" in a
  post-baseline tier
- Sprint 6 partially closes — issues #15/#16/#17 shipped are real
  and stay closed. Issue #18 (center HUD) gets marked superseded
  by the voice-first pivot; will be re-scoped when ambient-mode
  work happens
- Sprint 7 (Professionalization — tests, CI, docstrings, ADRs)
  continues as planned. Actually more valuable now.
- Sprint 8+ needs re-planning around tiers, not around
  dashboard-panel features

## Open questions parked for later

Not blocking Tier 1 but worth flagging so they don't get forgotten:

- **Auto-launch on login** — how FRIDAY starts with the laptop. Not
  a Tier 1 concern, but a real Tier 3+ concern once voice is up.
- **Boot greeting flow** — the current Tauri boot sequence is a
  visual concept, not a voice one. Needs redesign for voice-first.
- **When ambient mode auto-triggers** — do I have to say "sleep" or
  does FRIDAY infer conversation-ended? Design question for whenever
  ambient mode gets built.
- **Screen-vision privacy model** — before this ever ships, needs
  real thought about what FRIDAY can and can't see, when it looks,
  and how I know it's looking.

## References

- Source design document: the "voice-first AI agent" prompt from
  August 25, 2026 (in Preetam's records)
- Iron Man / Avengers films: reference material for the personality
  and interaction model