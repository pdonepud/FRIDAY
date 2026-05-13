console.log("[FRIDAY UI] Frontend loaded.");

// Boot sequence orchestrator
// Timeline (all relative to page load):
//   0.0s — letter-reveal animation starts (CSS keyframes, staggered per-letter)
//   1.5s — show "INITIALIZING SYSTEMS"
//   2.2s — switch to "ESTABLISHING SECURE CONNECTION"
//   2.9s — switch to "ONLINE — WELCOME, PREETAM"
//   3.7s — fade out boot, fade in app
//   4.3s — boot fully hidden, app ready

const STATUS_MESSAGES = [
  "INITIALIZING SYSTEMS",
  "ESTABLISHING SECURE CONNECTION",
  "ONLINE — WELCOME, PREETAM",
];

function showStatusMessage(text) {
  const el = document.getElementById("boot-status");
  if (!el) return;
  el.classList.remove("visible");
  // Force reflow so the fade-out actually triggers
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

  // After CSS transition completes, fully remove boot from layout
  setTimeout(() => {
    boot.classList.add("boot-hidden");
  }, 600);
}

function startBootSequence() {
  // 1.5s into page lifetime — letters are done revealing
  setTimeout(() => showStatusMessage(STATUS_MESSAGES[0]), 1500);
  setTimeout(() => showStatusMessage(STATUS_MESSAGES[1]), 2200);
  setTimeout(() => showStatusMessage(STATUS_MESSAGES[2]), 2900);
  setTimeout(() => fadeOutBoot(), 3700);
}

// Start once DOM is ready
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", startBootSequence);
} else {
  startBootSequence();
}
