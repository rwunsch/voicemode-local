import test from "node:test";
import assert from "node:assert/strict";
import model from "../ui/pill/model.js";
const { viewModel, VOICES, GERMAN_VOICES, LANGUAGES, nextHidden } = model;

const snap = (o = {}) => ({
  visible: true, phase: "speaking", floor: { pid: 1, label: "a" },
  sessions: [], voice: "af_sky", language: "English",
  barge: { enabled: true, available: true, count: 3 }, platform: "windows", ui: {}, ...o,
});

test("disconnected after 3s with no snapshot", () => {
  const vm = viewModel(snap(), 1000, 4001);
  assert.equal(vm.connected, false);
  assert.equal(vm.label, "Disconnected");
  assert.equal(vm.primary.visible, false);
  assert.equal(vm.end.enabled, false);
  assert.equal(vm.stop.enabled, false);
  assert.equal(vm.pickersEnabled, false);
  assert.equal(vm.barge.enabled, false);
  assert.equal(vm.minimizeEnabled, true);
  assert.equal("dismissEnabled" in vm, false); // no X: _ minimizes, Quit is in the tray menu
  assert.equal(viewModel(snap(), 1000, 3999).connected, true);
  assert.equal(viewModel(null, null, 5).label, "Disconnected");
});

test("phase labels", () => {
  const l = (phase) => viewModel(snap({ phase }), 0, 0).label;
  assert.deepEqual(["speaking", "listening", "working", "paused"].map(l),
    ["Speaking", "Listening", "Between turns", "Paused"]);
});

test("pause button reads Resume when paused", () => {
  const p = viewModel(snap({ phase: "paused" }), 0, 0).primary;
  assert.equal(p.text, "Resume");
  assert.equal(p.cmd, "resume");
  const q = viewModel(snap(), 0, 0).primary;
  assert.equal(q.text, "Pause");
  assert.equal(q.cmd, "pause");
});

test("the Orb pauses during listening independently of the classic Done action", () => {
  const listening = viewModel(snap({ phase: "listening", orb: true }), 0, 0);
  assert.equal(listening.primary.cmd, "done");
  assert.equal(listening.orbControls.pause.cmd, "pause");
  assert.equal(listening.orbControls.pause.enabled, true);
  assert.equal(listening.orbControls.end.cmd, "end_voice");
  const muted = viewModel(snap({ phase: "mic-muted", orb: true }), 0, 0);
  assert.equal(muted.inTurn, true);
  assert.equal(muted.orbControls.pause.enabled, true);
  assert.equal(viewModel(snap({ phase: "paused", orb: true }), 0, 0).orbControls.pause.cmd, "resume");
});

test("stop needs a floor holder", () => {
  assert.equal(viewModel(snap({ floor: null }), 0, 0).stop.enabled, false);
  assert.equal(viewModel(snap(), 0, 0).stop.enabled, true);
});

test("barge switch hidden when unavailable", () => {
  const vm = viewModel(snap({ barge: { enabled: false, available: false, count: 0 } }), 0, 0);
  assert.equal(vm.barge.visible, false);
  assert.equal(viewModel(snap(), 0, 0).barge.visible, true);
  assert.equal(viewModel(snap(), 0, 0).barge.on, true);
});

test("interruption count wording", () => {
  const t = (count) => viewModel(snap({ barge: { enabled: true, available: true, count } }), 0, 0).barge.countText;
  assert.deepEqual([t(0), t(1), t(3)],
    ["0 interruptions today", "1 interruption today", "3 interruptions today"]);
});

test("keys match platform", () => {
  assert.deepEqual(viewModel(snap({ platform: "windows" }), 0, 0).keys,
    [{ keys: ["Ctrl", "Space"], text: "tap: voice off, hold: talk" }]);
  assert.deepEqual(viewModel(snap({ platform: "linux" }), 0, 0).keys,
    [{ keys: ["Ctrl", "Space"], text: "tap: voice off, hold: talk" }]);
  const mac = viewModel(snap({ platform: "mac" }), 0, 0).keys;
  assert.deepEqual(mac.map((k) => k.keys.join("-")), ["Control-Option-Space", "Control-Option-Escape"]);
  assert.equal(mac[0].text, "tap: voice off, hold: talk");
});

test("a held push-to-talk key shows Holding", () => {
  const vm = viewModel(snap({ phase: "holding" }), 0, 0);
  assert.equal(vm.label, "Holding\u2026");
  assert.equal(vm.stateText, "Holding\u2026");
});

test("queue row shows position", () => {
  const vm = viewModel(snap({ sessions: [
    { pid: 1, label: "voice-barge", state: "talking", position: null },
    { pid: 2, label: "felix", state: "waiting", position: 1 },
    { pid: 3, label: null, state: "waiting", position: 2 },
    { pid: 4, label: "w", state: "working", position: null }] }), 0, 0);
  assert.deepEqual(vm.queue.map((r) => [r.name, r.text]), [
    ["voice-barge", "talking"], ["felix", "waiting, 1st"], ["session 3", "waiting, 2nd"], ["w", "working"]]);
});

test("dot and visibility come from ui and snapshot", () => {
  assert.equal(viewModel(snap({ ui: { minimized: true } }), 0, 0).mode, "dot");
  assert.equal(viewModel(snap({ ui: { expanded: true } }), 0, 0).mode, "expanded");
  assert.equal(viewModel(snap({ ui: { docked: true, side: "left" } }), 0, 0).mode, "rail");
  assert.equal(viewModel(snap({ ui: { docked: true, side: "left" } }), 0, 0).side, "left");
  assert.equal(viewModel(snap({ ui: { docked: true, dockOpen: true } }), 0, 0).mode, "docked");
  assert.equal(viewModel(snap({ ui: { docked: true, dockOpen: true, tucked: true } }), 0, 0).mode, "tab");
  assert.equal(viewModel(snap({ ui: { docked: true, tucked: true } }), 0, 0).mode, "tab");
  assert.equal(viewModel(snap({ ui: { docked: true, tucked: true, side: "left" } }), 0, 0).side, "left");
  assert.equal(viewModel(snap({ ui: { tucked: true } }), 0, 0).mode, "pill"); // only a docked pill can tuck
  assert.equal(viewModel(snap(), 0, 0).mode, "pill");
});

test("pickers expose constant lists and current value", () => {
  assert.ok(VOICES.includes("af_sky") && !VOICES.includes("dm_martin") && !VOICES.includes("p_de_thorsten"));
  assert.deepEqual(LANGUAGES, ["English", "German"]);
  assert.equal(viewModel(snap(), 0, 0).voice, "af_sky");
});

test("disconnected does not show last state", () => {
  const vm = viewModel(snap(), 0, 10000);
  assert.equal(vm.phase, "disconnected");
  assert.deepEqual(vm.queue, []);
  assert.equal(vm.barge.visible, false);
});

test("pause and resume carry icons with the text variation selector", () => {
  assert.equal(viewModel(snap(), 0, 0).primary.icon, "\u23F8\uFE0E");
  assert.equal(viewModel(snap({ phase: "paused" }), 0, 0).primary.icon, "\u23EF\uFE0E"); // ⏯, not ▶
});

test("pickers enabled only with a floor", () => {
  assert.equal(viewModel(snap(), 0, 0).pickersEnabled, true);
  assert.equal(viewModel(snap({ floor: null, voice: null }), 0, 0).pickersEnabled, false);
});

test("unknown voice stays null; the language falls back to English", () => {
  const vm = viewModel(snap({ voice: null, language: null }), 0, 0);
  assert.equal(vm.voice, null);
  assert.equal(vm.language, "English");
});

test("language is derived from a German voice when the snapshot has none", () => {
  assert.equal(viewModel(snap({ voice: "dm_martin", language: null }), 0, 0).language, "German");
  assert.equal(viewModel(snap({ voice: "p_de_eva", language: null }), 0, 0).language, "German");
  assert.equal(viewModel(snap({ voice: "am_puck", language: null }), 0, 0).language, "English");
  assert.equal(viewModel(snap({ voice: "dm_martin", language: "English" }), 0, 0).language, "English");
});

test("the voice list follows the language", () => {
  const en = viewModel(snap({ voice: "af_sky", language: "English" }), 0, 0).voices;
  assert.deepEqual(en, VOICES);
  assert.ok(!en.some((v) => v.startsWith("dm_") || v.startsWith("p_de_")));
  const de = viewModel(snap({ voice: "dm_martin", language: "German" }), 0, 0).voices;
  assert.deepEqual(de, ["dm_martin", "p_de_thorsten", "p_de_eva"]);
  assert.deepEqual(GERMAN_VOICES, de);
  // a current voice outside the list is still shown
  assert.deepEqual(viewModel(snap({ voice: "af_sky", language: "German" }), 0, 0).voices, [...de, "af_sky"]);
  // empty with no floor and no voice: still the English list, never an empty select
  assert.deepEqual(viewModel(snap({ floor: null, voice: null, language: null }), 0, 0).voices, VOICES);
});

test("minimize and dismiss stay enabled when connected too", () => {
  const vm = viewModel(snap(), 0, 0);
  assert.equal(vm.minimizeEnabled, true);
  assert.equal("dismissEnabled" in vm, false); // no X: _ minimizes, Quit is in the tray menu
});

test("nextHidden: un-hides on false to true", () => {
  assert.equal(nextHidden(true, false, true, false), false);
});
test("nextHidden: un-hides on first visible snapshot after disconnect", () => {
  assert.equal(nextHidden(true, true, true, true), false);
  assert.equal(nextHidden(true, null, true, true), false);
});
test("nextHidden: visible:false alone does not un-hide", () => {
  assert.equal(nextHidden(true, true, false, false), true);
  assert.equal(nextHidden(true, false, false, false), true);
  assert.equal(nextHidden(true, true, false, true), true);
});
test("nextHidden: visible:true right after a connected dismiss stays hidden", () => {
  assert.equal(nextHidden(true, true, true, false), true);
});
test("nextHidden: not hidden stays not hidden", () => {
  assert.equal(nextHidden(false, true, true, false), false);
  assert.equal(nextHidden(false, true, false, false), false);
});

test("session name and state text are separate lines", () => {
  const sessions = [{ pid: 1, label: "voice-barge", state: "talking" }, { pid: 2, label: "x", state: "waiting", position: 1 }, { pid: 3, label: "y", state: "working" }];
  const vm = viewModel(snap({ phase: "speaking", floor: { pid: 1, label: "voice-barge" }, sessions }), 0, 0);
  assert.equal(vm.session, "voice-barge");
  assert.equal(vm.stateText, "Speaking");
  assert.equal(vm.waitingText, "+1 waiting");
  const alone = viewModel(snap({ phase: "listening", floor: { pid: 1, label: "voice-barge" }, sessions: sessions.slice(0, 1) }), 0, 0);
  assert.equal(alone.stateText, "Listening");
  assert.equal(alone.waitingText, "");
  assert.equal(viewModel(snap({ floor: null }), 0, 0).session, "");
  assert.equal(viewModel(snap(), 0, 99999).session, "");
  assert.equal(viewModel(snap(), 0, 99999).stateText, "Disconnected");
});

test("a current voice missing from VOICES is added to the list", () => {
  const a = viewModel(snap({ voice: "am_onyx" }), 0, 0);
  assert.ok(a.voices.includes("am_onyx"));
  assert.equal(a.voices.filter((v) => v === "am_onyx").length, 1);
  const b = viewModel(snap({ voice: "bf_isabella" }), 0, 0);
  assert.ok(b.voices.includes("bf_isabella") && !b.voices.includes("am_onyx"));
  const known = viewModel(snap({ voice: "af_sky" }), 0, 0).voices;
  assert.equal(known.length, VOICES.length);
  assert.equal(VOICES.includes("am_onyx"), false);
});

test("a stopped session reads Voice off and cannot be stopped again", () => {
  const vm = viewModel(snap({ phase: "stopped" }), 0, 0);
  assert.equal(vm.label, "Voice off");
  assert.equal(vm.end.visible, false);
  assert.equal(vm.stop.enabled, false);
  assert.equal(viewModel(snap({ phase: "speaking" }), 0, 0).stop.enabled, true);
});

test("panel lists at most ten sessions, talking first, then a count of the rest", () => {
  const sessions = [{ pid: 99, label: "busy", state: "working", position: null }];
  for (let i = 12; i >= 1; i--) sessions.push({ pid: i, label: "w" + i, state: "waiting", position: i });
  sessions.push({ pid: 50, label: "now", state: "talking", position: null });
  const vm = viewModel(snap({ sessions }), 0, 0);
  assert.equal(vm.queue.length, 10);
  assert.deepEqual(vm.queue.slice(0, 3).map((r) => r.name), ["now", "w1", "w2"]);
  assert.equal(vm.queueMore, "+4 more queued");
  assert.equal(viewModel(snap({ sessions: sessions.slice(0, 3) }), 0, 0).queueMore, "");
});

test("beeps switch shows the backend's value", () => {
  assert.equal(viewModel(snap({ beeps: true }), 0, 0).beeps.on, true);
  assert.equal(viewModel(snap({ beeps: false }), 0, 0).beeps.on, false);
  assert.equal(viewModel(null, 0, 0).beeps.on, false);
});

const wakeTarget = { label: "demo" };




test("no Skip anywhere in the model", () => {
  const vm = viewModel(snap(), 0, 0);
  assert.equal("interrupt" in vm, false);
  assert.equal("play" in vm, false);
});

test("a Wake result replaces the state text briefly", () => {
  const vm = viewModel(snap({ phase: "working", wake: { text: "Woke demo", ok: true } }), 0, 0);
  assert.equal(vm.stateText, "Woke demo");
  assert.equal(vm.wakeText, "Woke demo");
  const bad = viewModel(snap({ phase: "working", wake: { text: "Can't wake demo: busy", ok: false } }), 0, 0);
  assert.equal(bad.stateText, "Can't wake demo: busy");
  assert.equal(viewModel(snap({ phase: "working", wake: null }), 0, 0).stateText, "Between turns");
  assert.equal(viewModel(snap({ wake: { text: "x" } }), 1000, 5000).wakeText, "");
});

// Talos review, finding 7: German only where a German voice can speak.
test("German and its voices are dropped when the backend says German is unavailable", () => {
  const vm = viewModel(snap({ german_available: false }), 0, 0);
  assert.deepEqual(vm.languages, ["English"]);
  assert.ok(!vm.voices.some((v) => GERMAN_VOICES.includes(v)));
  const de = viewModel(snap({ german_available: false, voice: "af_sky", language: "German" }), 0, 0);
  assert.deepEqual(de.voices, VOICES); // never the German list
  assert.deepEqual(de.languages, ["English", "German"]); // its own value stays listed (final review)
});

test("German stays offered when available, or when an older backend does not say", () => {
  assert.deepEqual(viewModel(snap({ german_available: true }), 0, 0).languages, ["English", "German"]);
  assert.deepEqual(viewModel(snap(), 0, 0).languages, LANGUAGES);
  assert.deepEqual(viewModel(snap({ german_available: true, voice: "dm_martin", language: "German" }), 0, 0).voices,
    GERMAN_VOICES);
});

// Talos review, finding 8: no push-to-talk keys where no listener runs.
test("no push-to-talk keys when the backend says no listener runs", () => {
  for (const platform of ["windows", "linux", "mac"]) {
    assert.deepEqual(viewModel(snap({ platform, ptt: false }), 0, 0).keys, [], platform);
  }
  assert.deepEqual(viewModel(snap({ platform: "windows", ptt: true }), 0, 0).keys,
    [{ keys: ["Ctrl", "Space"], text: "tap: voice off, hold: talk" }]);
  assert.equal(viewModel(snap({ platform: "mac", ptt: true }), 0, 0).keys.length, 2);
});

// Talos review 2026-11-12: pill-windows.ps1 posts {"window":{"dropped":cmd}} when a command
// cannot reach a restarting backend. Treated as a snapshot, it made the page look connected
// for 3 more seconds; it must mark the page disconnected at once.
test("incoming messages are told apart: restore, dropped, other window message, snapshot", () => {
  const { incomingKind } = model;
  assert.equal(incomingKind({ cmd: "restore" }), "restore");
  assert.equal(incomingKind({ cmd: "quit" }), "quit");
  assert.equal(incomingKind({ window: { dropped: "stop" } }), "dropped");
  assert.equal(incomingKind({ window: { dropped: "" } }), "dropped");
  assert.equal(incomingKind({ window: { mode: "pill" } }), "window");
  assert.equal(incomingKind(snap()), "snapshot");
  assert.equal(incomingKind(null), "ignore");
  assert.equal(incomingKind("x"), "ignore");
});

// Final review minor: a session already in German, with German not available, showed a
// select whose value was not in its own list. Keep German listed while it is the value.
test("German stays listed while it is the session's language, even when not available", () => {
  const vm = viewModel(snap({ language: "German", german_available: false }), 0, 0);
  assert.ok(vm.languages.includes("German"));
  const en = viewModel(snap({ language: "English", german_available: false }), 0, 0);
  assert.deepEqual(en.languages, ["English"]);
});

test("a barge click shows the wanted state at once, as pending, until the backend agrees", () => {
  const { bargeShown, PENDING_MS } = model;
  // no click: the backend's state
  assert.deepEqual(bargeShown(true, null, 0), { on: true, pending: false, keep: null });
  // clicked off at t=1000, backend still on: shown off, pending
  const p = { on: false, at: 1000 };
  assert.deepEqual(bargeShown(true, p, 1500), { on: false, pending: true, keep: p });
  // backend agrees: pending ends
  assert.deepEqual(bargeShown(false, p, 1800), { on: false, pending: false, keep: null });
  // backend never agrees: after PENDING_MS the real state shows again
  assert.deepEqual(bargeShown(true, p, 1000 + PENDING_MS + 1), { on: true, pending: false, keep: null });
});

test("On then Off before the backend moved: the switch stays Off while the backend passes through On", () => {
  // Talos on FELIX #1813 (v1.2.8 head): the pending Off matched the cached Off snapshot and was
  // dropped, so the backend's transient On flipped the switch back against the last click.
  const { bargeShown, clickBarge } = model;
  let pending = clickBarge(false, null, 1000);          // On clicked; backend still Off
  assert.equal(bargeShown(false, pending, 1100).on, true);
  pending = clickBarge(false, bargeShown(false, pending, 1200).keep, 1200);   // then Off
  let s = bargeShown(false, pending, 1300);             // backend has not moved yet
  assert.deepEqual([s.on, s.pending], [false, true]);
  s = bargeShown(true, s.keep, 1500);                   // backend applies the first wish: On
  assert.deepEqual([s.on, s.pending], [false, true]);
  s = bargeShown(false, s.keep, 1900);                  // then the last wish: Off
  assert.deepEqual([s.on, s.pending, s.keep], [false, false, null]);
});

test("a single click still clears as soon as the backend agrees", () => {
  const { bargeShown, clickBarge } = model;
  const p = clickBarge(true, null, 1000);               // Off clicked while backend On
  assert.deepEqual([bargeShown(true, p, 1100).on, bargeShown(true, p, 1100).pending], [false, true]);
  assert.deepEqual(bargeShown(false, p, 1300), { on: false, pending: false, keep: null });
});

test("the German voice list is the backend's german_voices when it sends one", () => {
  const vm = viewModel(snap({ german_available: true, voice: "p_de_thorsten", language: "German",
                              german_voices: ["p_de_thorsten", "p_de_eva"] }), 0, 0);
  assert.deepEqual(vm.voices, ["p_de_thorsten", "p_de_eva"]);
  const old = viewModel(snap({ german_available: true, voice: "p_de_thorsten", language: "German" }), 0, 0);
  assert.deepEqual(old.voices, ["dm_martin", "p_de_thorsten", "p_de_eva"]);   // older backend: as before
});

// 2026-11-16 (Robert): every local voice and language, from the backend's voices_by_language.
const VBL = {
  Spanish: ["ef_dora", "em_alex"], English: ["af_sky", "bm_george"], Dutch: ["p_nl_nathalie"],
  German: ["p_de_thorsten", "p_de_eva"], French: ["ff_siwis"],
};

test("languages and voices come from voices_by_language: English first, then alphabetical", () => {
  const vm = viewModel(snap({ voices_by_language: VBL }), 0, 0);
  assert.deepEqual(vm.languages, ["English", "Dutch", "French", "German", "Spanish"]);
  assert.deepEqual(vm.voices, ["af_sky", "bm_george"]);
  const es = viewModel(snap({ voices_by_language: VBL, voice: "ef_dora", language: "Spanish" }), 0, 0);
  assert.deepEqual(es.voices, ["ef_dora", "em_alex"]);
  // the current voice stays listed even when its server does not list it
  const odd = viewModel(snap({ voices_by_language: VBL, voice: "af_river", language: "English" }), 0, 0);
  assert.deepEqual(odd.voices, ["af_sky", "bm_george", "af_river"]);
  // the session's language stays listed, so the select shows its real value
  const it = viewModel(snap({ voices_by_language: VBL, voice: "if_sara", language: "Italian" }), 0, 0);
  assert.ok(it.languages.includes("Italian"));
  assert.deepEqual(it.voices, ["if_sara"]);
});

test("without English the languages are alphabetical; German follows the list, not german_available", () => {
  const vm = viewModel(snap({ voices_by_language: { Spanish: ["ef_dora"], Dutch: ["p_nl_nathalie"] },
    voice: "ef_dora", language: "Spanish", german_available: true }), 0, 0);
  assert.deepEqual(vm.languages, ["Dutch", "Spanish"]);
});

// VOICEMODE_PILL_LANGUAGES: the backend sends only the listed languages in voices_by_language
// (and german_available false when German is not listed); the menu shows exactly those.
test("an English-only voices_by_language gives an English-only menu", () => {
  const vm = viewModel(snap({ voices_by_language: { English: ["af_sky", "bm_george"] },
    german_available: false, voice: "af_sky", language: "English" }), 0, 0);
  assert.deepEqual(vm.languages, ["English"]);
  assert.deepEqual(vm.voices, ["af_sky", "bm_george"]);
  const two = viewModel(snap({ voices_by_language: { French: ["ff_siwis"], English: ["af_sky"] } }), 0, 0);
  assert.deepEqual(two.languages, ["English", "French"]);
});

test("absent or empty voices_by_language: today's lists", () => {
  for (const vbl of [undefined, null, {}, [], "x"]) {
    const vm = viewModel(snap({ voices_by_language: vbl }), 0, 0);
    assert.deepEqual(vm.languages, LANGUAGES);
    assert.deepEqual(vm.voices, VOICES);
    const de = viewModel(snap({ voices_by_language: vbl, voice: "dm_martin", language: "German",
      german_voices: ["dm_martin"] }), 0, 0);
    assert.deepEqual(de.voices, ["dm_martin"]);
  }
});

test("languageOf knows every Kokoro prefix and Piper's language code", () => {
  const { languageOf } = model;
  assert.equal(languageOf("ef_dora"), "Spanish");
  assert.equal(languageOf("ff_siwis"), "French");
  assert.equal(languageOf("hm_omega"), "Hindi");
  assert.equal(languageOf("im_nicola"), "Italian");
  assert.equal(languageOf("jf_alpha"), "Japanese");
  assert.equal(languageOf("pm_alex"), "Portuguese");
  assert.equal(languageOf("zf_xiaobei"), "Chinese");
  assert.equal(languageOf("dm_martin"), "German");
  assert.equal(languageOf("bm_george"), "English");
  assert.equal(languageOf("p_nl_nathalie"), "Dutch");
  assert.equal(languageOf("p_de_eva"), "German");
  assert.equal(languageOf("p_ko_hana"), "Korean");
  assert.equal(languageOf("p_pl_gosia"), "Polish");
  assert.equal(languageOf("p_ru_dmitri"), "Russian");
  assert.equal(languageOf("xx_unknown"), "English");
  assert.equal(languageOf(null), "English");
  assert.equal(viewModel(snap({ voice: "ef_dora", language: null }), 0, 0).language, "Spanish");
});

test("voiceForLanguage keeps the current voice in that language, else picks its first voice", () => {
  const { voiceForLanguage } = model;
  assert.equal(voiceForLanguage(VBL, "Spanish", "af_sky"), "ef_dora");
  assert.equal(voiceForLanguage(VBL, "Spanish", "em_alex"), "em_alex");
  assert.equal(voiceForLanguage(VBL, "English", "af_sky"), "af_sky");
  assert.equal(voiceForLanguage(VBL, "Italian", "af_sky"), null);   // not listed: nothing to send
  assert.equal(voiceForLanguage(undefined, "German", "af_sky"), null);
  assert.equal(voiceForLanguage({}, "German", "af_sky"), null);
});

test("app.js sends the language's voice before the language", async () => {
  const { readFile } = await import("node:fs/promises");
  const app = await readFile(new URL("../ui/pill/app.js", import.meta.url), "utf8");
  const body = app.split('$("language").onchange = function', 2)[1].split("};", 1)[0];
  assert.ok(body.includes("M.voiceForLanguage("), body);
  const voiceAt = body.indexOf('cmd: "set_voice"'), langAt = body.indexOf('cmd: "set_language"');
  assert.ok(voiceAt >= 0 && langAt > voiceAt, body);
});

// -- 2026-11-17, a tester's feedback (n=1): controls easy to find, a coloured indicator of who
// is speaking and when an interruption took effect, a voice preview.

test("labelled buttons: text, not icon-only, for both spots", () => {
  const vm = viewModel(snap({ phase: "speaking" }), 0, 0);
  assert.deepEqual([vm.primary.text, vm.end.text], ["Pause", "Voice off"]);
  const w = viewModel(snap({ phase: "working", play: { label: "felix" } }), 0, 0).primary;
  assert.deepEqual([w.text, w.cmd, w.hint], ["Wake", "wake", "Wake felix"]);
});

test("indicator tone per phase, with a text label for every tone", () => {
  const t = (phase) => viewModel(snap({ phase }), 0, 0).indicator;
  assert.deepEqual(["speaking", "listening", "holding", "working", "idle", "stopped", "paused"].map((p) => t(p).tone),
    ["speaking", "listening", "neutral", "neutral", "neutral", "neutral", "muted"]);
  assert.equal(t("speaking").text, "Assistant speaking");
  assert.equal(t("listening").text, "Listening to you");
  assert.equal(t("paused").text, "Paused");
  for (const p of ["speaking", "listening", "holding", "working", "idle", "stopped", "paused"]) {
    assert.ok(t(p).text, p);
  }
  const off = viewModel(null, null, 5).indicator;
  assert.deepEqual([off.tone, off.text], ["off", "Disconnected"]);
});

test("indicator names the floor session", () => {
  assert.equal(viewModel(snap({ floor: { pid: 1, label: "felix" } }), 0, 0).indicator.who, "felix");
  assert.equal(viewModel(snap({ floor: null }), 0, 0).indicator.who, "");
});

test("interruptFlash: a rising count flashes for 1.5 s; first sight, same or lower count does not", () => {
  const { interruptFlash, FLASH_MS } = model;
  assert.equal(FLASH_MS, 1500);
  assert.equal(interruptFlash(3, 4, 10000, null), 11500);
  assert.equal(interruptFlash(3, 5, 10000, null), 11500);
  assert.equal(interruptFlash(null, 4, 10000, null), null);   // the first snapshot: nothing new
  assert.equal(interruptFlash(4, 4, 10000, 9000), 9000);       // unchanged: keep what was set
  assert.equal(interruptFlash(4, 0, 10000, null), null);       // the day rolled over
  assert.equal(interruptFlash(4, undefined, 10000, null), null);
  assert.equal(interruptFlash(4, 5, 10000, 10500), 11500);     // a second one restarts it
});

test("an interruption shows Interrupted while the flash lasts, then the phase again", () => {
  const s = snap({ phase: "listening" });
  const vm = viewModel(s, 0, 1000, { interruptedUntil: 2500 });
  assert.deepEqual([vm.indicator.tone, vm.indicator.text, vm.stateText], ["interrupted", "Interrupted", "Interrupted"]);
  const after = viewModel(s, 0, 2500, { interruptedUntil: 2500 });
  assert.deepEqual([after.indicator.tone, after.stateText], ["listening", "Listening"]);
  assert.equal(viewModel(null, null, 1000, { interruptedUntil: 2500 }).indicator.tone, "off");
});

test("a preview answer is its own kind of incoming message", () => {
  const { incomingKind } = model;
  assert.equal(incomingKind({ window: { preview: "busy", reason: "turn" } }), "preview");
  assert.equal(incomingKind({ window: { dropped: "preview_voice" } }), "dropped");
  assert.equal(incomingKind({ window: { other: 1 } }), "window");
});

test("previewView: what the preview button and its line say for each answer", () => {
  const { previewView, PREVIEW_SHOW_MS, PREVIEW_PENDING_MS } = model;
  const at = 1000;
  const v = (a, now = at, connected = true) => {
    const r = previewView(a && Object.assign({ at }, a), now, connected, false);
    return { text: r.text, pending: r.pending, enabled: r.enabled };
  };
  assert.deepEqual(v(null), { text: "", pending: false, enabled: true });
  assert.deepEqual(v({ preview: "loading", voice: "am_adam" }), { text: "Preparing a sample…", pending: true, enabled: false });
  assert.deepEqual(v({ preview: "playing", voice: "am_adam" }), { text: "Playing am_adam…", pending: true, enabled: false });
  assert.deepEqual(v({ preview: "done", voice: "am_adam" }), { text: "", pending: false, enabled: true });
  assert.equal(v({ preview: "busy", reason: "turn" }).text, "Preview works between voice turns");
  assert.equal(v({ preview: "busy", reason: "preview" }).text, "A sample is already playing");
  assert.equal(v({ preview: "stopped" }).text, "Sample stopped: a voice turn started");
  assert.equal(v({ preview: "unavailable", detail: "no audio output module (sounddevice)" }).text,
    "Preview unavailable: no audio output module (sounddevice)");
  assert.equal(v({ preview: "error", detail: "unknown voice: x" }).text, "Preview failed: unknown voice: x");
  assert.equal(v({ preview: "busy", reason: "turn" }).enabled, true);
  // a result stays readable for a while, then the line clears
  assert.equal(v({ preview: "error", detail: "x" }, at + PREVIEW_SHOW_MS - 1).text, "Preview failed: x");
  assert.equal(v({ preview: "error", detail: "x" }, at + PREVIEW_SHOW_MS).text, "");
  // a request with no answer frees the button again
  const lost = v({ preview: "loading" }, at + PREVIEW_PENDING_MS);
  assert.deepEqual([lost.pending, lost.enabled, lost.text], [false, true, "No answer from the preview"]);
  // disconnected: no preview
  assert.equal(v(null, at, false).enabled, false);
});

test("previewView: the button rests during a voice turn, and says why", () => {
  const { previewView } = model;
  const inTurn = previewView(null, 0, true, true);
  assert.deepEqual([inTurn.enabled, inTurn.title], [false, "Preview works between voice turns"]);
  const between = previewView(null, 0, true, false);
  assert.deepEqual([between.enabled, between.title], [true, "Hear a short sample of this voice"]);
});

test("viewModel says when a voice turn is on, for the preview button", () => {
  const t = (phase) => viewModel(snap({ phase }), 0, 0).inTurn;
  assert.deepEqual(["speaking", "listening", "holding", "paused", "working", "idle", "stopped"].map(t),
    [true, true, true, true, false, false, false]);
  assert.equal(viewModel(null, null, 5).inTurn, false);
});

test("app.js sends preview_voice with the voice shown in the list", async () => {
  const { readFile } = await import("node:fs/promises");
  const app = await readFile(new URL("../ui/pill/app.js", import.meta.url), "utf8");
  const body = app.split('$("preview").onclick = function', 2)[1].split("\n  };", 1)[0];
  assert.ok(body.includes('cmd: "preview_voice", voice: $("voice").value'), body);
});

test("a stuck voice server reads Not responding, not Speaking (FELIX #2095)", () => {
  const vm = viewModel(snap({ phase: "not_responding" }), 0, 0);
  assert.equal(vm.label, "Not responding");
  assert.deepEqual([vm.primary.visible, vm.end.visible], [false, false]);
  assert.equal(vm.inTurn, false);
  assert.equal(vm.indicator.tone, "interrupted");
  assert.match(vm.indicator.text, /not responding/i);
});


// FELIX #2094 (Robert, 2026-11-27) and the adversarial UX review: two spots in a fixed
// place. Left (primary) changes with the phase: Pause / Done / Resume / Wake / Resume voice.
// Right (end) is always "Voice off", so it never jumps under a pointer aimed at Pause when a
// reply ends. Done shows the return arrow (Robert) and sends "done", which the backend
// forwards only while the server is listening.
const lay = (o) => { const vm = viewModel(snap(o), 0, 0);
  return [vm.primary.visible ? vm.primary.kind : null, vm.end.visible ? vm.end.kind : null]; };

test("fixed layout per phase: primary changes, Voice off stays on the right", () => {
  assert.deepEqual(lay({ phase: "speaking" }), ["pause", "voiceoff"]);
  assert.deepEqual(lay({ phase: "holding" }), ["pause", "voiceoff"]);
  assert.deepEqual(lay({ phase: "listening" }), ["done", "voiceoff"]);
  assert.deepEqual(lay({ phase: "paused" }), ["resume", "voiceoff"]);
  assert.deepEqual(lay({ phase: "working", play: wakeTarget }), ["wake", null]);
  assert.deepEqual(lay({ phase: "idle", play: wakeTarget }), ["wake", null]);
  assert.deepEqual(lay({ phase: "working" }), [null, "voiceoff"]);   // nobody to wake
  assert.deepEqual(lay({ phase: "stopped", play: wakeTarget }), ["resumevoice", null]);
  assert.deepEqual(lay({ phase: "stopped" }), [null, null]);
  assert.deepEqual(lay({ phase: "not_responding" }), [null, null]);
});

test("Done: return arrow, sends done, says what it does", () => {
  const p = viewModel(snap({ phase: "listening" }), 0, 0).primary;
  assert.deepEqual([p.cmd, p.icon, p.text, p.enabled], ["done", "\u21B5\uFE0E", "Done", true]);
  assert.equal(p.hint, "Done: send what you said now");
});

test("Voice off: square, sends stop, never a toggle", () => {
  const e = viewModel(snap({ phase: "speaking" }), 0, 0).end;
  assert.deepEqual([e.cmd, e.icon, e.text, e.toggle], ["stop", "\u25A0\uFE0E", "Voice off", false]);
  assert.equal(e.hint, "Voice off: continue in text");
});

test("Pause and Resume keep their pressed state; hints explain them", () => {
  const p = viewModel(snap({ phase: "speaking" }), 0, 0).primary;
  assert.deepEqual([p.cmd, p.text, p.toggle, p.pressed], ["pause", "Pause", true, false]);
  assert.match(p.hint, /mic|wait/i);
  const r = viewModel(snap({ phase: "paused" }), 0, 0).primary;
  assert.deepEqual([r.cmd, r.text, r.icon, r.pressed], ["resume", "Resume", "\u23EF\uFE0E", true]);
});

test("stopped: says voice is off and how to resume", () => {
  const vm = viewModel(snap({ phase: "stopped" }), 0, 0);
  assert.match(vm.stateText, /Voice off/);
  assert.match(vm.stateText, /ask for voice/i);
  const w = viewModel(snap({ phase: "stopped", play: wakeTarget }), 0, 0).primary;
  assert.deepEqual([w.cmd, w.text], ["wake", "Resume voice"]);
});

test("not responding: no buttons, says it will stop and to restart the chat", () => {
  const vm = viewModel(snap({ phase: "not_responding" }), 0, 0);
  assert.match(vm.stateText, /will stop itself\. Then restart the chat/);
  assert.doesNotMatch(vm.stateText, /restarts/);   // no promise nobody measured (FELIX-voice)
});

test("no floor: the buttons are shown but disabled", () => {
  const vm = viewModel(snap({ phase: "listening", floor: null }), 0, 0);
  assert.deepEqual([vm.primary.enabled, vm.end.enabled], [false, false]);
});

test("disconnected: nothing to press", () => {
  const vm = viewModel(null, null, 5);
  assert.deepEqual([vm.primary.visible, vm.end.enabled], [false, false]);
});

test("keyboard hint says voice off, not stop", () => {
  for (const platform of ["mac", "windows", "linux"]) {
    const keys = viewModel(snap({ platform }), 0, 0).keys;
    assert.match(keys[0].text, /tap: voice off, hold: talk/, platform);
  }
  const mac = viewModel(snap({ platform: "mac" }), 0, 0).keys;
  assert.equal(mac[1].text, "voice off");
});

// Robert 2026-11-27 (1a): Pause stays reachable while you talk, in the panel only: it closes
// the mic and throws away what was said (a phone call, someone walking in), voice stays on.
test("listening: the panel offers Pause mic; other phases do not", () => {
  const m = viewModel(snap({ phase: "listening" }), 0, 0).mic;
  assert.deepEqual([m.visible, m.cmd, m.text, m.enabled], [true, "pause", "Pause mic", true]);
  assert.match(m.hint, /discard|throw/i);
  for (const phase of ["speaking", "paused", "working", "stopped"]) {
    assert.equal(viewModel(snap({ phase }), 0, 0).mic.visible, false, phase);
  }
  assert.equal(viewModel(null, null, 5).mic.visible, false);
});

// Robert 2026-11-27 (1b): the close button shrinks the pill to the tray / menu bar while a
// session lives; quitting the pill is in that icon's menu (wrapper sends {"cmd":"quit"}).
test("close shrinks to the tray; its hint says where Quit is", () => {
  for (const platform of ["windows", "mac"]) {
    const c = viewModel(snap({ platform }), 0, 0).close;
    assert.equal(c.action, "minimize", platform);
    assert.match(c.hint, platform === "mac" ? /click the dot/ : /right-click the icon/, platform);
    assert.match(c.hint, /quit/);
    assert.ok(c.hint.length <= 40, c.hint); // FELIX-MAC: the status line cut a longer one
  }
});

// FELIX #2107: after N s of listening the backend sets still_listening; the status line then
// says how to end the turn. Text only: a sound would land in the recording.
test("still listening: the status line points at Done", () => {
  const vm = model.viewModel(snap({ phase: "listening", still_listening: true }), 0, 0);
  assert.equal(vm.stateText, model.STILL_LISTENING_TEXT);
  assert.match(vm.stateText, /Done/);
  assert.equal(vm.primary.kind, "done");
  const plain = model.viewModel(snap({ phase: "listening" }), 0, 0);
  assert.notEqual(plain.stateText, model.STILL_LISTENING_TEXT);
  const speaking = model.viewModel(snap({ phase: "speaking", still_listening: true }), 0, 0);
  assert.notEqual(speaking.stateText, model.STILL_LISTENING_TEXT);
});
