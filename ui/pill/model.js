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
  var STOP = "\u25A0\uFE0E", DONE = "\u21B5\uFE0E" /* return arrow (Robert 2026-11-27) */;
  // Hover hints: shown in the status line too, since a Mac panel that never takes focus shows
  // no native tooltip (title).
  var HINTS = { pause: "Pause: hold voice; the mic closes and the reply waits",
    resume: "Resume: continue the voice turn", done: "Done: send what you said now",
    voiceoff: "Voice off: continue in text", resumevoice: "Resume voice: listen to you again",
    pausemic: "Pause mic: close the mic and discard what was said; voice stays on" };
  function closeHint(platform) {
    return platform === "mac" ? "Minimize to the menu bar; click the dot there for Show or Quit"
      : "Minimize to the tray; Quit is in its menu (right-click)";
  }
  var LABELS = { speaking: "Speaking", listening: "Listening", holding: "Holding\u2026", working: "Between turns",
    idle: "Not in a voice turn", paused: "Paused",
    stopped: "Voice off", disconnected: "Disconnected", not_responding: "Not responding" };
  // The status line's longer text where the short label would leave the user guessing.
  var STATE_TEXT = { stopped: "Voice off: continuing in text. Ask for voice to resume",
    // It ends itself after VOICEMODE_LOOP_STALL_EXIT_S; whether the host starts a new one is
    // not known (Copilot desktop unmeasured, FELIX's launch.js does not respawn): no promise.
    not_responding: "Voice server not responding: it will stop itself. Then restart the chat" };

  // The coloured indicator (2026-11-17): who is speaking, as a tone and always a text label too.
  var TONES = { speaking: "speaking", listening: "listening", holding: "neutral", working: "neutral", idle: "neutral",
    stopped: "neutral", paused: "muted", disconnected: "off", not_responding: "interrupted" };
  var TONE_TEXT = { speaking: "Assistant speaking", listening: "Listening to you", paused: "Paused",
    interrupted: "Interrupted", disconnected: "Disconnected",
    // FELIX #2095: the voice server's loop is stuck; it ends itself after 90 s.
    not_responding: "Voice server not responding" };
  function indicatorFor(phase, who, interrupted) {
    if (phase === "disconnected") return { tone: "off", text: TONE_TEXT.disconnected, who: "" };
    if (interrupted) return { tone: "interrupted", text: TONE_TEXT.interrupted, who: who };
    return { tone: TONES[phase] || "neutral", text: TONE_TEXT[phase] || LABELS[phase] || LABELS.idle, who: who };
  }
  // Two spots, always in the same place (FELIX #2094, UX review 2026-11-27): the left one
  // (primary) changes with the phase; the right one (end) is always Voice off, so it never
  // moves under a pointer aimed at Pause as a reply ends.
  function button(kind, cmd, text, icon, enabled, extra) {
    return Object.assign({ kind: kind, cmd: cmd, text: text, icon: icon, enabled: enabled,
      visible: true, hint: HINTS[kind] || text, toggle: false, pressed: false }, extra || {});
  }
  // A click on a button whose meaning changed less than this long ago is ignored (app.js).
  var GUARD_MS = 500;
  var HIDDEN = { kind: null, cmd: null, text: "", icon: "", enabled: false, visible: false, hint: "",
    toggle: false, pressed: false };
  // When barge.count rises between two snapshots, "Interrupted" shows until the returned time.
  // The first snapshot (prev null), the same count, or a lower one (a new day) change nothing.
  var FLASH_MS = 1500;
  function interruptFlash(prevCount, count, now, until) {
    if (typeof prevCount === "number" && typeof count === "number" && count > prevCount) return now + FLASH_MS;
    return until == null ? null : until;
  }

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
      return [{ keys: ["Control", "Option", "Space"], text: "tap: voice off, hold: talk" },
        { keys: ["Control", "Option", "Escape"], text: "voice off" }];
    }
    // Same rule everywhere (2026-11-07): a tap under 0.8 s stops, back to text; a hold talks.
    return [{ keys: ["Ctrl", "Space"], text: "tap: voice off, hold: talk" }];
  }

  function viewModel(snapshot, lastSeenMs, nowMs, extra) {
    var connected = !!snapshot && lastSeenMs != null && nowMs - lastSeenMs <= STALE_MS;
    var off = { enabled: false };
    if (!connected) {
      // Never show the last known state: a stale "Listening" is worse than none.
      return { connected: false, phase: "disconnected", label: LABELS.disconnected, mode: "pill", side: null,
        primary: HIDDEN, end: button("voiceoff", "stop", "Voice off", STOP, false), mic: HIDDEN, stop: off,
        close: { action: "minimize", hint: closeHint(snapshot && snapshot.platform) },
        indicator: indicatorFor("disconnected", "", false),
        inTurn: false, pickersEnabled: false, minimizeEnabled: true, queue: [], queueMore: "", voice: null, language: null, keys: [], session: "", stateText: LABELS.disconnected, waitingText: "",
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
    var inTurn = phase === "speaking" || phase === "listening" || phase === "holding" || phase === "paused";
    var wakeable = !!s.play && (phase === "idle" || phase === "working" || phase === "stopped");
    var floorOn = !!s.floor;
    var wakeTitle = "Wake " + (s.play && s.play.label || "the last session");
    var primary = phase === "listening" ? button("done", "done", "Done", DONE, floorOn)
      : phase === "speaking" || phase === "holding"
        ? button("pause", "pause", "Pause", PAUSE, floorOn, { toggle: true })
      : phase === "paused" ? button("resume", "resume", "Resume", RESUME, true, { toggle: true, pressed: true })
      : wakeable && phase === "stopped" ? button("resumevoice", "wake", "Resume voice", "", true, { title: wakeTitle })
      : wakeable ? button("wake", "wake", "Wake", "", true, { hint: wakeTitle, title: wakeTitle })
      : HIDDEN;
    // Voice off: in a turn, or between turns when nobody can be woken (one button between
    // turns, never Wake and Voice off together: Robert 2026-11-07).
    var endShown = inTurn || (!wakeable && floorOn && (phase === "working" || phase === "idle"));
    var end = endShown ? button("voiceoff", "stop", "Voice off", STOP, floorOn) : HIDDEN;
    var wakeText = s.wake && s.wake.text ? s.wake.text : "";
    var interrupted = !!(extra && extra.interruptedUntil != null && nowMs < extra.interruptedUntil);
    var mode = ui.minimized ? "dot" : ui.docked ? (ui.tucked ? "tab" : ui.dockOpen ? "docked" : "rail") : ui.expanded ? "expanded" : "pill";
    return {
      connected: true, phase: phase, inTurn: inTurn, label: LABELS[phase] || LABELS.idle, mode: mode,
      side: mode === "docked" || mode === "rail" || mode === "tab" ? (ui.side === "left" ? "left" : "right") : null,
      primary: primary,
      end: end,
      // Panel only (Robert 2026-11-27): while you talk, Pause is the mute for an interruption.
      mic: phase === "listening" ? button("pausemic", "pause", "Pause mic", PAUSE, floorOn) : HIDDEN,
      // The close button shrinks to the tray / menu bar; Quit is in that icon's menu.
      close: { action: "minimize", hint: closeHint(s.platform) },
      indicator: indicatorFor(phase, name, interrupted),
      stop: { enabled: floorOn && phase !== "stopped" },
      pickersEnabled: !!s.floor, minimizeEnabled: true,
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
      stateText: interrupted ? TONE_TEXT.interrupted : wakeText || STATE_TEXT[phase] || LABELS[phase] || LABELS.idle,
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

  // The voice preview's line and button from the backend's last answer (voice_pill.py _preview).
  // answer: {preview, reason, detail, voice, at} or null. A result stays PREVIEW_SHOW_MS; a
  // request with no answer frees the button after PREVIEW_PENDING_MS.
  var PREVIEW_SHOW_MS = 6000, PREVIEW_PENDING_MS = 45000;
  function previewView(answer, now, connected, inTurn) {
    var a = answer || {}, age = now - (a.at || 0), state = a.preview;
    var pending = state === "loading" || state === "playing";
    var text = "";
    if (pending && age >= PREVIEW_PENDING_MS) {
      pending = false;
      if (age < PREVIEW_PENDING_MS + PREVIEW_SHOW_MS) text = "No answer from the preview";
    } else if (state === "loading") text = "Preparing a sample\u2026";
    else if (state === "playing") text = "Playing " + (a.voice || "the sample") + "\u2026";
    else if (age < PREVIEW_SHOW_MS) {
      if (state === "busy") text = a.reason === "preview" ? "A sample is already playing" : "Preview works between voice turns";
      else if (state === "stopped") text = "Sample stopped: a voice turn started";
      else if (state === "unavailable") text = "Preview unavailable: " + (a.detail || "no audio output here");
      else if (state === "error") text = "Preview failed: " + (a.detail || "unknown error");
    }
    return { text: text, pending: pending, enabled: !!connected && !pending && !inTurn,
      title: inTurn ? "Preview works between voice turns" : "Hear a short sample of this voice" };
  }

  // What an incoming message is: the tray's "restore", a command the window could not
  // deliver ("dropped": the backend is restarting), another window message, or a snapshot.
  function incomingKind(s) {
    if (!s || typeof s !== "object") return "ignore";
    if (s.cmd === "restore") return "restore";
    if (s.cmd === "quit") return "quit";   // "Quit pill" in the tray / menu bar icon's menu
    if (s.window && typeof s.window === "object") {
      if ("dropped" in s.window) return "dropped";
      return "preview" in s.window ? "preview" : "window";
    }
    return "snapshot";
  }

  var api = { previewView: previewView, PREVIEW_SHOW_MS: PREVIEW_SHOW_MS, PREVIEW_PENDING_MS: PREVIEW_PENDING_MS,
    interruptFlash: interruptFlash, FLASH_MS: FLASH_MS, GUARD_MS: GUARD_MS, clickBarge: clickBarge, bargeShown: bargeShown, PENDING_MS: PENDING_MS, incomingKind: incomingKind, nextHidden: nextHidden, viewModel: viewModel, languageOf: languageOf, voiceForLanguage: voiceForLanguage, VOICES: VOICES, GERMAN_VOICES: GERMAN_VOICES, LANGUAGES: LANGUAGES, STALE_MS: STALE_MS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window !== "undefined") window.PillModel = api;
})();
