// Pure view logic for the voice pill. No DOM. Classic script so it loads from file://
// in a WebView, and CommonJS-exported so `node --test` can import it.
(function () {
  var STALE_MS = 3000, MAX_ROWS = 10;
  var VOICES = ["af_sky", "af_bella", "af_heart", "af_jessica", "af_nicole", "af_nova", "af_sarah", "af_alloy",
    "am_adam", "am_echo", "am_eric", "am_michael", "am_liam", "am_puck", "am_fenrir",
    "bf_alice", "bf_emma", "bf_lily", "bm_daniel", "bm_george", "bm_lewis"];
  var GERMAN_VOICES = ["dm_martin", "p_de_thorsten", "p_de_eva"];
  var LANGUAGES = ["English", "German"];
  function languageOf(voice) { return voice && (voice.indexOf("dm_") === 0 || voice.indexOf("p_de_") === 0) ? "German" : "English"; }
  var PAUSE = "\u23F8\uFE0E", RESUME = "\u25B6\uFE0E";
  var LABELS = { speaking: "Speaking", listening: "Listening", holding: "Holding\u2026", working: "Between turns",
    idle: "Not in a voice turn", paused: "Paused", stopped: "Stopped", disconnected: "Disconnected" };

  function ordinal(n) {
    var s = ["th", "st", "nd", "rd"], v = n % 100;
    return n + (s[(v - 20) % 10] || s[v] || s[0]);
  }

  function rowRank(r) {
    if (r.state === "talking") return 0;
    return r.state === "waiting" ? r.position || 1e6 : 2e6;
  }

  function keysFor(platform) {
    if (platform === "mac") {
      return [{ keys: ["Control", "Option", "Space"], text: "tap: stop, hold: talk" },
        { keys: ["Control", "Option", "Escape"], text: "stop" }];
    }
    // Same rule everywhere (2026-11-07): a tap under 0.8 s stops, back to text; a hold talks.
    return [{ keys: ["Ctrl", "Space"], text: "tap: stop, hold: talk" }];
  }

  function viewModel(snapshot, lastSeenMs, nowMs) {
    var connected = !!snapshot && lastSeenMs != null && nowMs - lastSeenMs <= STALE_MS;
    var off = { enabled: false };
    if (!connected) {
      // Never show the last known state: a stale "Listening" is worse than none.
      return { connected: false, phase: "disconnected", label: LABELS.disconnected, mode: "pill", side: null,
        interrupt: off, pause: { enabled: false, label: "Pause", cmd: "pause", icon: PAUSE }, stop: off,
        pickersEnabled: false, minimizeEnabled: true, dismissEnabled: true, queue: [], queueMore: "", voice: null, language: null, keys: [], session: "", stateText: LABELS.disconnected, waitingText: "",
        barge: { visible: false, on: false, enabled: false, countText: "" }, beeps: { on: false }, voices: VOICES, languages: LANGUAGES };
    }
    var s = snapshot, phase = s.phase || "idle", ui = s.ui || {}, barge = s.barge || {};
    var count = barge.count || 0;
    var rows = (s.sessions || []).map(function (r, i) { return { r: r, i: i }; }).sort(function (a, b) {
      return rowRank(a.r) - rowRank(b.r) || a.i - b.i; // whoever is talking first, then the queue in order
    }).map(function (x) { return x.r; });
    var waiting = (s.sessions || []).filter(function (r) { return r.state === "waiting"; }).length;
    var name = s.floor ? (s.floor.label || "") : "";
    var language = s.language || languageOf(s.voice);
    var base = language === "German" ? GERMAN_VOICES : VOICES;
    var voices = s.voice && base.indexOf(s.voice) < 0 ? base.concat([s.voice]) : base;
    var mode = ui.minimized ? "dot" : ui.docked ? (ui.tucked ? "tab" : ui.dockOpen ? "docked" : "rail") : ui.expanded ? "expanded" : "pill";
    return {
      connected: true, phase: phase, label: LABELS[phase] || LABELS.idle, mode: mode,
      side: mode === "docked" || mode === "rail" || mode === "tab" ? (ui.side === "left" ? "left" : "right") : null,
      interrupt: { enabled: phase === "speaking" || phase === "listening" },
      pause: { enabled: true, label: phase === "paused" ? "Resume" : "Pause",
        cmd: phase === "paused" ? "resume" : "pause", icon: phase === "paused" ? RESUME : PAUSE },
      stop: { enabled: !!s.floor && phase !== "stopped" },
      pickersEnabled: !!s.floor, minimizeEnabled: true, dismissEnabled: true,
      queue: rows.slice(0, MAX_ROWS).map(function (r) {
        var text = r.state === "waiting" && r.position ? "waiting, " + ordinal(r.position) : r.state;
        return { name: r.label || "session " + r.pid, text: text, state: r.state };
      }),
      queueMore: rows.length > MAX_ROWS ? "+" + (rows.length - MAX_ROWS) + " more queued" : "",
      voice: s.voice || null, language: language,
      keys: keysFor(s.platform),
      barge: { visible: !!barge.available, on: !!barge.enabled, enabled: true,
        countText: count + (count === 1 ? " interruption today" : " interruptions today") },
      beeps: { on: !!s.beeps },
      voices: voices, languages: LANGUAGES,
      session: name,
      stateText: LABELS[phase] || LABELS.idle,
      waitingText: waiting ? "+" + waiting + " waiting" : "",
    };
  }

  // Whether a dismissed page stays hidden after a snapshot. The wrapper hides from `visible`;
  // the page only decides when its own hidden flag clears.
  function nextHidden(hidden, prevVisible, snapshotVisible, wasStale) {
    if (!hidden) return false;
    return !(snapshotVisible === true && (wasStale || prevVisible === false));
  }

  var api = { nextHidden: nextHidden, viewModel: viewModel, VOICES: VOICES, GERMAN_VOICES: GERMAN_VOICES, LANGUAGES: LANGUAGES, STALE_MS: STALE_MS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window !== "undefined") window.PillModel = api;
})();
