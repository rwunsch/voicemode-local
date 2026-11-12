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
  assert.equal(vm.main.enabled, false);
  assert.equal(vm.pause.enabled, false);
  assert.equal(vm.stop.enabled, false);
  assert.equal(vm.pickersEnabled, false);
  assert.equal(vm.barge.enabled, false);
  assert.deepEqual([vm.minimizeEnabled, vm.dismissEnabled], [true, true]);
  assert.equal(viewModel(snap(), 1000, 3999).connected, true);
  assert.equal(viewModel(null, null, 5).label, "Disconnected");
});

test("phase labels", () => {
  const l = (phase) => viewModel(snap({ phase }), 0, 0).label;
  assert.deepEqual(["speaking", "listening", "working", "paused"].map(l),
    ["Speaking", "Listening", "Between turns", "Paused"]);
});

test("pause button reads Resume when paused", () => {
  const p = viewModel(snap({ phase: "paused" }), 0, 0).pause;
  assert.equal(p.label, "Resume");
  assert.equal(p.cmd, "resume");
  const q = viewModel(snap(), 0, 0).pause;
  assert.equal(q.label, "Pause");
  assert.equal(q.cmd, "pause");
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
    [{ keys: ["Ctrl", "Space"], text: "tap: stop, hold: talk" }]);
  assert.deepEqual(viewModel(snap({ platform: "linux" }), 0, 0).keys,
    [{ keys: ["Ctrl", "Space"], text: "tap: stop, hold: talk" }]);
  const mac = viewModel(snap({ platform: "mac" }), 0, 0).keys;
  assert.deepEqual(mac.map((k) => k.keys.join("-")), ["Control-Option-Space", "Control-Option-Escape"]);
  assert.equal(mac[0].text, "tap: stop, hold: talk");
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
  assert.equal(viewModel(snap(), 0, 0).pause.icon, "\u23F8\uFE0E");
  assert.equal(viewModel(snap({ phase: "paused" }), 0, 0).pause.icon, "\u23EF\uFE0E"); // ⏯, not ▶
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
  assert.deepEqual([vm.minimizeEnabled, vm.dismissEnabled], [true, true]);
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

test("a stopped session reads Stopped and cannot be stopped again", () => {
  const vm = viewModel(snap({ phase: "stopped" }), 0, 0);
  assert.equal(vm.label, "Stopped");
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
const buttons = (o) => { const vm = viewModel(snap(o), 0, 0);
  return { main: vm.main.kind, mainOn: vm.main.enabled, pause: vm.pause.visible, resume: vm.pause.cmd }; };

test("between turns: Wake only; in a turn: Stop + Pause; paused: Stop + Resume", () => {
  for (const phase of ["idle", "working", "stopped"]) {
    const b = buttons({ phase, play: wakeTarget });
    assert.deepEqual([b.main, b.mainOn, b.pause], ["wake", true, false], phase);
  }
  for (const phase of ["speaking", "listening", "holding"]) {
    const b = buttons({ phase, play: wakeTarget });
    assert.deepEqual([b.main, b.pause, b.resume], ["stop", true, "pause"], phase);
  }
  const p = viewModel(snap({ phase: "paused", play: wakeTarget }), 0, 0);
  assert.deepEqual([p.main.kind, p.pause.visible, p.pause.cmd, p.pause.icon], ["stop", true, "resume", "\u23EF\uFE0E"]);
});

test("the main button is one element that switches: wake or stop, never both", () => {
  const w = viewModel(snap({ phase: "idle", play: wakeTarget }), 0, 0).main;
  assert.deepEqual([w.kind, w.cmd, w.title], ["wake", "wake", "Wake demo"]);
  const s = viewModel(snap({ phase: "speaking", play: wakeTarget }), 0, 0).main;
  assert.deepEqual([s.kind, s.cmd, s.title], ["stop", "stop", "Stop"]);
});

test("without a wakeable session the main button is a disabled-by-floor Stop, not Wake", () => {
  for (const o of [{ phase: "idle", play: null }, { phase: "idle" }]) {
    const m = viewModel(snap(o), 0, 0).main;
    assert.equal(m.kind, "stop");
  }
  assert.equal(viewModel(snap({ phase: "idle", play: null, floor: null }), 0, 0).main.enabled, false);
  const d = viewModel(snap({ phase: "idle", play: wakeTarget }), 1000, 5000);
  assert.deepEqual([d.main.kind, d.main.enabled, d.pause.visible], ["stop", false, false]); // disconnected
});

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
  assert.deepEqual(de.languages, ["English"]);
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
    [{ keys: ["Ctrl", "Space"], text: "tap: stop, hold: talk" }]);
  assert.equal(viewModel(snap({ platform: "mac", ptt: true }), 0, 0).keys.length, 2);
});
