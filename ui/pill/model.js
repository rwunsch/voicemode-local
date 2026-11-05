// Pure view logic for the voice pill. No DOM. Classic script so it loads from file://
// in a WebView, and CommonJS-exported so `node --test` can import it.
(function () {
  var STALE_MS = 3000;
  var VOICES = ["af_sky", "af_bella", "af_heart", "af_jessica", "af_nicole", "af_nova", "af_sarah", "af_alloy",
    "am_adam", "am_echo", "am_eric", "am_michael", "am_liam", "am_puck", "am_fenrir",
    "bf_alice", "bf_emma", "bf_lily", "bm_daniel", "bm_george", "bm_lewis",
    "dm_martin", "p_de_thorsten"];
  var LANGUAGES = ["English", "German"];
  var PAUSE = "\u23F8\uFE0E", RESUME = "\u25B6\uFE0E";
  var LABELS = { speaking: "Speaking", listening: "Listening", working: "Between turns",
    idle: "Not in a voice turn", paused: "Paused", disconnected: "Disconnected" };

  function ordinal(n) {
    var s = ["th", "st", "nd", "rd"], v = n % 100;
    return n + (s[(v - 20) % 10] || s[v] || s[0]);
  }

  function keysFor(platform) {
    if (platform === "mac") {
      return [{ keys: ["Control", "Option", "Space"], text: "tap: skip, hold: talk" },
        { keys: ["Control", "Option", "Escape"], text: "stop" }];
    }
    return [{ keys: ["Ctrl", "Space"], text: "tap: skip, hold: talk" }];
  }

  function viewModel(snapshot, lastSeenMs, nowMs) {
    var connected = !!snapshot && lastSeenMs != null && nowMs - lastSeenMs <= STALE_MS;
    var off = { enabled: false };
    if (!connected) {
      // Never show the last known state: a stale "Listening" is worse than none.
      return { connected: false, phase: "disconnected", label: LABELS.disconnected, mode: "pill", side: null,
        interrupt: off, pause: { enabled: false, label: "Pause", cmd: "pause", icon: PAUSE }, stop: off,
        pickersEnabled: false, minimizeEnabled: true, dismissEnabled: true, queue: [], voice: null, language: null, keys: [], subText: "",
        barge: { visible: false, on: false, enabled: false, countText: "" }, voices: VOICES, languages: LANGUAGES };
    }
    var s = snapshot, phase = s.phase || "idle", ui = s.ui || {}, barge = s.barge || {};
    var count = barge.count || 0;
    var waiting = (s.sessions || []).filter(function (r) { return r.state === "waiting"; }).length;
    var name = s.floor ? (s.floor.label || "") : "";
    var voices = s.voice && VOICES.indexOf(s.voice) < 0 ? VOICES.concat([s.voice]) : VOICES;
    var mode = ui.minimized ? "dot" : ui.docked ? (ui.tucked ? "tab" : ui.dockOpen ? "docked" : "rail") : ui.expanded ? "expanded" : "pill";
    return {
      connected: true, phase: phase, label: LABELS[phase] || LABELS.idle, mode: mode,
      side: mode === "docked" || mode === "rail" || mode === "tab" ? (ui.side === "left" ? "left" : "right") : null,
      interrupt: { enabled: phase === "speaking" || phase === "listening" },
      pause: { enabled: true, label: phase === "paused" ? "Resume" : "Pause",
        cmd: phase === "paused" ? "resume" : "pause", icon: phase === "paused" ? RESUME : PAUSE },
      stop: { enabled: !!s.floor },
      pickersEnabled: !!s.floor, minimizeEnabled: true, dismissEnabled: true,
      queue: (s.sessions || []).map(function (r) {
        var text = r.state === "waiting" && r.position ? "waiting, " + ordinal(r.position) : r.state;
        return { name: r.label || "session " + r.pid, text: text, state: r.state };
      }),
      voice: s.voice || null, language: s.language || null,
      keys: keysFor(s.platform),
      barge: { visible: !!barge.available, on: !!barge.enabled, enabled: true,
        countText: count + (count === 1 ? " interruption today" : " interruptions today") },
      voices: voices, languages: LANGUAGES,
      subText: name && waiting ? name + " \u00b7 +" + waiting + " waiting" : name,
    };
  }

  // Whether a dismissed page stays hidden after a snapshot. The wrapper hides from `visible`;
  // the page only decides when its own hidden flag clears.
  function nextHidden(hidden, prevVisible, snapshotVisible, wasStale) {
    if (!hidden) return false;
    return !(snapshotVisible === true && (wasStale || prevVisible === false));
  }

  var api = { nextHidden: nextHidden, viewModel: viewModel, VOICES: VOICES, LANGUAGES: LANGUAGES, STALE_MS: STALE_MS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window !== "undefined") window.PillModel = api;
})();
