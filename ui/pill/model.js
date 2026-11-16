// Pure view logic for the voice pill. No DOM. Classic script so it loads from file://
// in a WebView, and CommonJS-exported so `node --test` can import it.
(function () {
  var STALE_MS = 3000, MAX_ROWS = 10;
  var VOICES = ["af_sky", "af_bella", "af_heart", "af_jessica", "af_nicole", "af_nova", "af_sarah", "af_alloy",
    "am_adam", "am_echo", "am_eric", "am_michael", "am_liam", "am_puck", "am_fenrir",
    "bf_alice", "bf_emma", "bf_lily", "bm_daniel", "bm_george", "bm_lewis"];
  var GERMAN_VOICES = ["dm_martin", "p_de_thorsten", "p_de_eva"];
  var LANGUAGES = ["English", "German"];
  // A voice's language from its id: Piper's p_<code>_..., else Kokoro's first letter; else English.
  var KOKORO_LANGUAGES = { a: "English", b: "English", e: "Spanish", f: "French", h: "Hindi", i: "Italian",
    j: "Japanese", p: "Portuguese", z: "Chinese", d: "German" };
  var PIPER_LANGUAGES = { de: "German", nl: "Dutch", pl: "Polish", ru: "Russian", ko: "Korean" };
  function languageOf(voice) {
    if (typeof voice !== "string" || !voice) return "English";
    var parts = voice.split("_");
    if (parts[0] === "p" && parts.length >= 3) return PIPER_LANGUAGES[parts[1]] || "English";
    return KOKORO_LANGUAGES[voice.charAt(0)] || "English";
  }
  // The backend's {language: [voice ids]} from the local TTS servers, or null when it has none
  // (an older backend, or no server answered): then the lists above are used, as before.
  function usableVbl(vbl) {
    if (!vbl || typeof vbl !== "object" || Array.isArray(vbl)) return null;
    var keys = Object.keys(vbl).filter(function (k) { return Array.isArray(vbl[k]) && vbl[k].length; });
    return keys.length ? keys : null;
  }
  // The voice to send with a chosen language: the current one if it is in that language's list,
  // else the list's first voice; null when the language has no list (send no voice).
  function voiceForLanguage(vbl, language, current) {
    var list = usableVbl(vbl) && Array.isArray(vbl[language]) ? vbl[language] : null;
    if (!list || !list.length) return null;
    return list.indexOf(current) >= 0 ? current : list[0];
  }
  var PAUSE = "\u23F8\uFE0E", RESUME = "\u23EF\uFE0E"  /* play/pause, not a plain play: no second play-like icon */;
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
        main: { kind: "stop", cmd: "stop", title: "Stop", enabled: false },
        pause: { visible: false, enabled: false, label: "Pause", cmd: "pause", icon: PAUSE }, stop: off,
        pickersEnabled: false, minimizeEnabled: true, dismissEnabled: true, queue: [], queueMore: "", voice: null, language: null, keys: [], session: "", stateText: LABELS.disconnected, waitingText: "",
        barge: { visible: false, on: false, enabled: false, countText: "" }, beeps: { on: false }, voices: VOICES, languages: LANGUAGES,
        wakeText: "" };
    }
    var s = snapshot, phase = s.phase || "idle", ui = s.ui || {}, barge = s.barge || {};
    var count = barge.count || 0;
    var rows = (s.sessions || []).map(function (r, i) { return { r: r, i: i }; }).sort(function (a, b) {
      return rowRank(a.r) - rowRank(b.r) || a.i - b.i; // whoever is talking first, then the queue in order
    }).map(function (x) { return x.r; });
    var waiting = (s.sessions || []).filter(function (r) { return r.state === "waiting"; }).length;
    var name = s.floor ? (s.floor.label || "") : "";
    var language = s.language || languageOf(s.voice);
    // German only where a German voice can speak; an older backend that does not say keeps it.
    var german = s.german_available !== false;
    // A session already in German keeps German listed, so the select shows its real value.
    var languages, base;
    var vblKeys = usableVbl(s.voices_by_language);
    if (vblKeys) {
      // Every local language, English first, then alphabetical; the session's own stays listed.
      languages = vblKeys.filter(function (k) { return k !== "English"; }).sort();
      if (vblKeys.indexOf("English") >= 0) languages.unshift("English");
      if (languages.indexOf(language) < 0) languages = languages.concat([language]);
      base = s.voices_by_language[language] || [];
      if (!base.length && !s.voice) base = s.voices_by_language[languages[0]] || []; // never an empty select
    } else {
      languages = german || language === "German" ? LANGUAGES : ["English"];
      // The German voices that can speak here (backend's german_voices); an older backend: all.
      var germanList = Array.isArray(s.german_voices) && s.german_voices.length ? s.german_voices : GERMAN_VOICES;
      base = german && language === "German" ? germanList : VOICES;
    }
    var voices = s.voice && base.indexOf(s.voice) < 0 ? base.concat([s.voice]) : base;
    // The main button is Wake between turns (idle, between turns, stopped) when the backend
    // can name a session to wake, and Stop otherwise. One button, never both. Pause/Resume
    // exist only during a voice turn. (There is no Skip: the backend "interrupt" command stays.)
    var inTurn = phase === "speaking" || phase === "listening" || phase === "holding" || phase === "paused";
    var wakeable = !!s.play && (phase === "idle" || phase === "working" || phase === "stopped");
    var stopOn = !!s.floor && phase !== "stopped";
    var main = wakeable
      ? { kind: "wake", cmd: "wake", title: "Wake " + (s.play.label || "the last session"), enabled: true }
      : { kind: "stop", cmd: "stop", title: "Stop", enabled: stopOn };
    var wakeText = s.wake && s.wake.text ? s.wake.text : "";
    var mode = ui.minimized ? "dot" : ui.docked ? (ui.tucked ? "tab" : ui.dockOpen ? "docked" : "rail") : ui.expanded ? "expanded" : "pill";
    return {
      connected: true, phase: phase, label: LABELS[phase] || LABELS.idle, mode: mode,
      side: mode === "docked" || mode === "rail" || mode === "tab" ? (ui.side === "left" ? "left" : "right") : null,
      main: main,
      pause: { visible: inTurn, enabled: true, label: phase === "paused" ? "Resume" : "Pause",
        cmd: phase === "paused" ? "resume" : "pause", icon: phase === "paused" ? RESUME : PAUSE },
      stop: { enabled: !!s.floor && phase !== "stopped" },
      pickersEnabled: !!s.floor, minimizeEnabled: true, dismissEnabled: true,
      queue: rows.slice(0, MAX_ROWS).map(function (r) {
        var text = r.state === "waiting" && r.position ? "waiting, " + ordinal(r.position) : r.state;
        return { name: r.label || "session " + r.pid, text: text, state: r.state };
      }),
      queueMore: rows.length > MAX_ROWS ? "+" + (rows.length - MAX_ROWS) + " more queued" : "",
      voice: s.voice || null, language: language,
      keys: s.ptt === false ? [] : keysFor(s.platform), // no listener runs: no shortcut to show
      barge: { visible: !!barge.available, on: !!barge.enabled, enabled: true,
        countText: count + (count === 1 ? " interruption today" : " interruptions today") },
      beeps: { on: !!s.beeps },
      voices: voices, languages: languages,
      session: name,
      stateText: wakeText || LABELS[phase] || LABELS.idle,
      wakeText: wakeText,
      waitingText: waiting ? "+" + waiting + " waiting" : "",
    };
  }

  // Whether a dismissed page stays hidden after a snapshot. The wrapper hides from `visible`;
  // the page only decides when its own hidden flag clears.
  function nextHidden(hidden, prevVisible, snapshotVisible, wasStale) {
    if (!hidden) return false;
    return !(snapshotVisible === true && (wasStale || prevVisible === false));
  }

  // A barge click: the switch shows the wanted state at once, marked pending, until a snapshot
  // agrees or PENDING_MS passes (Mac, 2026-11-13: the label waited 1-2 s for the next tick, and
  // a click with no visible change read as "I can't click it"). keep is what the page holds on to.
  var PENDING_MS = 5000;
  // A pending click ends when the backend has moved since the click and now agrees, or after
  // PENDING_MS. "Moved" matters for On-then-Off before the backend moved (Talos on FELIX #1813):
  // the Off matches the backend's old Off, but the backend still passes through On first.
  function bargeShown(enabled, pending, now) {
    enabled = !!enabled;
    if (!pending || now - pending.at > PENDING_MS) {
      return { on: enabled, pending: false, keep: null };
    }
    var from = "from" in pending ? !!pending.from : !pending.on;   // the backend at the first click
    var moved = !!pending.moved || enabled !== from;
    if (moved && enabled === !!pending.on) {
      return { on: enabled, pending: false, keep: null };
    }
    var keep = moved === !!pending.moved ? pending : Object.assign({}, pending, { moved: true });
    return { on: !!pending.on, pending: true, keep: keep };
  }

  // The pending state a click starts: the opposite of what the switch shows now. A click while an
  // earlier one is pending and the backend has not moved keeps that click's starting point.
  function clickBarge(enabled, pending, now) {
    var shown = bargeShown(enabled, pending, now);
    var held = shown.keep;
    var from = held && !held.moved ? ("from" in held ? !!held.from : !held.on) : !!enabled;
    return { on: !shown.on, at: now, from: from, moved: false };
  }

  // What an incoming message is: the tray's "restore", a command the window could not
  // deliver ("dropped": the backend is restarting), another window message, or a snapshot.
  function incomingKind(s) {
    if (!s || typeof s !== "object") return "ignore";
    if (s.cmd === "restore") return "restore";
    if (s.window && typeof s.window === "object") return "dropped" in s.window ? "dropped" : "window";
    return "snapshot";
  }

  var api = { clickBarge: clickBarge, bargeShown: bargeShown, PENDING_MS: PENDING_MS, incomingKind: incomingKind, nextHidden: nextHidden, viewModel: viewModel, languageOf: languageOf, voiceForLanguage: voiceForLanguage, VOICES: VOICES, GERMAN_VOICES: GERMAN_VOICES, LANGUAGES: LANGUAGES, STALE_MS: STALE_MS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window !== "undefined") window.PillModel = api;
})();
