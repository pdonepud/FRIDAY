# Sprint 10 Plan — FRIDAY

**Product:** FRIDAY (Personal AI Assistant)
**Team:** Solo — Preetam Donepudi
**Sprint completion date:** Planned — September 30, 2026
**Revision:** 1.0 — August 11, 2026

---

## Goal

Final polish before v1.0. This sprint is about the last-mile experience: a voice wake word (Porcupine) so FRIDAY responds to "Hey FRIDAY" without a hotkey, a proper installable `.exe` via `tauri build` so a new user can double-click to install, and a 60-second demo video for the portfolio.

By the end of this sprint FRIDAY should be something someone else can install and use — the definition of v1.0.

---

## Team Roles

| Role | Owner |
|------|-------|
| Product Owner | Preetam (self-directed scope decisions) |
| Tech Lead | Preetam (architectural decisions) |
| Backend Developer | Preetam |
| Frontend Developer | Preetam |
| DevOps / CI | Preetam |
| QA | Preetam (manual testing; unit tests planned for Sprint 7) |
| Scrum Master | Self-managed |

---

## Scrum Times

- **Weekly (Sunday)** — sprint progress review + backlog grooming
- **End of sprint** — retrospective doc + burnup chart
- **Ad-hoc** — architectural decisions logged in `docs/decisions/` as ADRs

---

## Task Listing

### Voice Wake Word (4 pts)

*Porcupine-based always-listening wake detection so the assistant is voice-triggerable.*

| Task | Estimate |
|------|----------|
| Sign up Picovoice free tier, obtain access key | 0.25 hr |
| Train custom "Hey FRIDAY" wake word via Picovoice console | 1 hr |
| `modules/wake.py` — background thread listening via PyAudio, callback on detection | 2 hr |
| Integration: wake triggers `--ask` mode with streaming Gemini reply | 1 hr |
| Toggle in dashboard settings + hotkey `Ctrl+Alt+W` to pause listening | 0.5 hr |

**Total: ~4.75 hrs**

### Packaging (3 pts)

*Ship an installable `.exe` — Tauri bundle + Python sidecar packaged as one artifact.*

| Task | Estimate |
|------|----------|
| Bundle Python runtime + `modules/` + `server/` via PyInstaller into a single sidecar binary | 2 hr |
| `tauri build` producing `.msi` installer for Windows | 1 hr |
| First-run wizard: prompt for API keys, store via OS keyring (Windows Credential Manager / macOS Keychain) — no `config.py` shipped in the packaged install | 1.5 hr |
| Install → launch → briefing playback test on a clean Windows VM | 1 hr |

**Total: ~5.5 hrs**

### Secrets Storage Design (DESIGN NOTE)

For dev, `config.py` (gitignored) is used, but this is a risk-reduction pattern rather than a guarantee:

- `.gitignore` blocks accidental `git add`, but a forced `git add -f` bypasses it
- Cloud-sync tools (OneDrive, iCloud, Dropbox) can silently mirror `config.py` off the developer's machine
- Backup tools may include the file

These risks are accepted for solo development. Rotation procedure (if a key does leak): revoke the leaked key at the provider, generate a new one, update `config.py`, and grep git history for any accidental commits.

For the packaged `.exe` distribution, `config.py` plaintext is NOT acceptable. The packaged app will use:

- **Windows:** Windows Credential Manager via `keyring` library
- **macOS:** macOS Keychain via `keyring` library
- **First-run flow:** Prompt for keys interactively, store via keyring
- **Rotation:** Documented procedure for updating a rotated key
- **Cleanup:** Uninstaller removes keyring entries
- **Backend verification:** on first run, verify `keyring.get_keyring()` returns a secure backend (Windows Credential Manager, macOS Keychain, or Linux Secret Service). If the fallback is `keyring.backends.fail.Keyring` or `PlaintextKeyring`, refuse to start and prompt the user to install a supported OS credential manager. Never silently fall back to plaintext.
- **Upgrade behavior:** credentials persist across FRIDAY version upgrades — the keyring namespace uses a stable service ID (`com.preetam.friday`) that doesn't change per-release.
- **Uninstall behavior:** the uninstaller offers two options — "Uninstall" (keeps credentials for future reinstall) and "Uninstall and remove credentials" (calls `keyring.delete_password` for every stored key).

This means the FRIDAY installer will NOT ship `config.py` at all — the first-run wizard writes credentials to the OS credential store. The Packaging tasks above have already been updated to reflect this: the first-run wizard task no longer writes `config.py`.

### Demo Video (2 pts)

*60-second portfolio walkthrough — cold launch through spoken briefing.*

| Task | Estimate |
|------|----------|
| Storyboard the 60 seconds: boot animation → dashboard → briefing → wake word demo | 0.5 hr |
| Screen recording + voiceover in OBS | 1.5 hr |
| Edit + captions + link from `README.md` and `CASE_STUDY.md` | 1 hr |

**Total: ~3 hrs**

---

## Initial Task Assignment

Solo project — all tasks assigned to Preetam.

---

## Sprint Retrospective

*Not yet started — this sprint is in the backlog.*

---

## Burnup Chart

Will be generated at sprint close.
