import test from "node:test";
import assert from "node:assert/strict";
import model from "../ui/pill/model.js";
const { viewModel, VOICES, LANGUAGES } = model;

const snap = (o = {}) => ({
  visible: true, phase: "speaking", floor: { pid: 1, label: "a" },
  sessions: [], voice: "af_sky", language: "English",
  barge: { enabled: true, available: true, count: 3 }, platform: "windows", ui: {}, ...o,
});

test("disconnected after 3s with no snapshot", () => {
  const vm = viewModel(snap(), 1000, 4001);
  assert.equal(vm.connected, false);
  assert.equal(vm.label, "Disconnected");
  assert.equal(vm.interrupt.enabled, false);
  assert.equal(vm.pause.enabled, false);
  assert.equal(vm.stop.enabled, false);
  assert.equal(vm.expandControlsEnabled, false);
  assert.equal(viewModel(snap(), 1000, 3999).connected, true);
  assert.equal(viewModel(null, null, 5).label, "Disconnected");
});

test("phase labels", () => {
  const l = (phase) => viewModel(snap({ phase }), 0, 0).label;
  assert.deepEqual(["speaking", "listening", "working", "paused"].map(l),
    ["Speaking", "Listening", "Not in a voice turn", "Paused"]);
});

test("pause button reads Resume when paused", () => {
  const p = viewModel(snap({ phase: "paused" }), 0, 0).pause;
  assert.equal(p.label, "Resume");
  assert.equal(p.cmd, "resume");
  const q = viewModel(snap(), 0, 0).pause;
  assert.equal(q.label, "Pause");
  assert.equal(q.cmd, "pause");
});

test("interrupt disabled unless speaking or listening", () => {
  const e = (phase) => viewModel(snap({ phase }), 0, 0).interrupt.enabled;
  assert.deepEqual(["speaking", "listening", "working", "paused", "idle"].map(e),
    [true, true, false, false, false]);
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
    [{ keys: ["Ctrl", "Space"], text: "tap: skip, hold: talk" }]);
  const mac = viewModel(snap({ platform: "mac" }), 0, 0).keys;
  assert.deepEqual(mac.map((k) => k.keys.join("-")), ["Control-Option-Space", "Control-Option-Escape"]);
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
  assert.equal(viewModel(snap({ ui: { docked: true, side: "left" } }), 0, 0).mode, "docked");
  assert.equal(viewModel(snap({ ui: { docked: true, side: "left" } }), 0, 0).side, "left");
  assert.equal(viewModel(snap(), 0, 0).mode, "pill");
});

test("pickers expose constant lists and current value", () => {
  assert.ok(VOICES.includes("dm_martin") && VOICES.includes("p_de_thorsten") && VOICES.includes("af_sky"));
  assert.deepEqual(LANGUAGES, ["English", "German"]);
  assert.equal(viewModel(snap(), 0, 0).voice, "af_sky");
});

test("disconnected does not show last state", () => {
  const vm = viewModel(snap(), 0, 10000);
  assert.equal(vm.phase, "disconnected");
  assert.deepEqual(vm.queue, []);
  assert.equal(vm.barge.visible, false);
});
