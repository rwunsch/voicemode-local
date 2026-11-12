// Renders the pill from model.js. Talks to the native wrapper only through
// window.pillTransport.send(json) and window.pillReceive(json).
// Window messages: {"window":{"mode":"pill"|"expanded"|"dot"|"docked"|"hidden","side":...}} and
// {"window":{"drag":true}}. "hidden" is sent once on Dismiss; the wrapper then hides the window.
(function () {
  var M = window.PillModel, $ = function (id) { return document.getElementById(id); };
  var hidden = false, snapshot = null, lastSeen = null, ui = null, lastMode = null, lastPhase = null, floorPid = null, filled = false;

  function send(obj) {
    try { if (window.pillTransport) window.pillTransport.send(JSON.stringify(obj)); } catch (e) { /* no transport */ }
  }
  function saveUi(patch) {
    ui = Object.assign({}, ui, patch);
    send({ cmd: "save_ui", ui: ui });
    render();
  }
  function fill(select, items, current) {
    var have = Array.prototype.map.call(select.options, function (o) { return o.value; });
    if (have.join("|") !== items.join("|")) { // first fill, or the floor session uses a voice not in the list
      var keep = select.value;
      select.replaceChildren();
      items.forEach(function (v) { var o = document.createElement("option"); o.value = o.textContent = v; select.appendChild(o); });
      select.value = keep;
    }
    if (select === document.activeElement) return;
    if (current) select.value = current; else select.selectedIndex = -1; // unknown is not shown as a value
  }
  function keysNode(k) {
    var d = document.createElement("div");
    k.keys.forEach(function (name, i) {
      if (i) d.appendChild(document.createTextNode(" "));
      var e = document.createElement("kbd"); e.textContent = name; d.appendChild(e);
    });
    d.appendChild(document.createTextNode(" " + k.text));
    return d;
  }

  function render() {
    var vm = M.viewModel(snapshot, lastSeen, Date.now());
    if (ui) { // local ui wins: it is what the user just chose, and the backend echoes it back
      var mode = ui.minimized ? "dot" : ui.docked ? (ui.tucked ? "tab" : ui.dockOpen ? "docked" : "rail") : ui.expanded ? "expanded" : "pill";
      vm.mode = mode; vm.side = mode === "docked" || mode === "rail" || mode === "tab" ? (ui.side || "right") : null;
    }
    document.body.dataset.mode = vm.mode;
    document.body.dataset.phase = vm.phase;
    $("label").textContent = vm.stateText;
    $("waiting").textContent = vm.waitingText;
    $("sub").textContent = vm.session || "Voice pill";
    var pid = snapshot && snapshot.floor ? snapshot.floor.pid : null;
    if (pid !== floorPid) { // a focused select is not refreshed: let go when the floor moves
      floorPid = pid;
      var f = document.activeElement;
      if (f === $("voice") || f === $("language")) document.activeElement.blur();
    }
    document.body.hidden = hidden;
    [["main", vm.main.enabled], ["pause", vm.pause.enabled],
      ["voice", vm.pickersEnabled], ["language", vm.pickersEnabled], ["barge", vm.barge.enabled],
      ["minimize", vm.minimizeEnabled], ["dismiss", vm.dismissEnabled], ["expand", true], ["dock", true]]
      .forEach(function (p) { $(p[0]).disabled = !p[1]; });
    // One button that switches: the sunrise (Wake) between turns, the square (Stop) in a turn.
    $("main").classList.toggle("wake", vm.main.kind === "wake");
    $("main").classList.toggle("stop", vm.main.kind === "stop");
    $("mainWake").hidden = vm.main.kind !== "wake"; $("mainStop").hidden = vm.main.kind !== "stop";
    $("main").title = vm.main.title; $("main").setAttribute("aria-label", vm.main.title);
    $("pause").hidden = !vm.pause.visible;
    $("pause").textContent = vm.pause.icon;
    $("pause").title = vm.pause.label; $("pause").setAttribute("aria-label", vm.pause.label);
    $("expand").setAttribute("aria-expanded", String(vm.mode === "expanded" || vm.mode === "docked"));
    $("dock").textContent = vm.mode === "docked" ? "Undock" : "Dock";
    $("tuck").textContent = vm.side === "left" ? "\u2039" : "\u203A";
    $("tuck").title = vm.mode === "docked" ? "Collapse" : "Slide out of view";
    $("tuck").setAttribute("aria-label", $("tuck").title);
    var rail = vm.mode === "rail";
    $("expand").textContent = rail ? (vm.side === "left" ? "\u203A" : "\u2039") : "\u2304";
    $("drag").title = [vm.session, vm.stateText, vm.waitingText].filter(Boolean).join(" \u00b7 ");
    $("tab").textContent = vm.side === "left" ? "\u203A" : "\u2039";
    fill($("voice"), vm.voices, vm.voice); fill($("language"), vm.languages, vm.language); filled = true;
    $("bargeRow").style.display = vm.barge.visible ? "" : "none";
    $("barge").textContent = vm.barge.on ? "On" : "Off";
    $("barge").setAttribute("aria-checked", String(vm.barge.on));
    $("beeps").textContent = vm.beeps.on ? "On" : "Off";
    $("beeps").setAttribute("aria-checked", String(vm.beeps.on));
    $("count").textContent = vm.barge.visible ? vm.barge.countText : "";
    var q = $("queue"); q.replaceChildren();
    vm.queue.forEach(function (r) {
      var d = document.createElement("div"); d.className = "q";
      var a = document.createElement("span"), b = document.createElement("span");
      a.textContent = r.name; b.textContent = r.text; b.className = "m"; d.append(a, b); q.appendChild(d);
    });
    if (vm.queueMore) {
      var more = document.createElement("div"); more.className = "q m"; more.textContent = vm.queueMore; q.appendChild(more);
    }
    $("keys").replaceChildren.apply($("keys"), vm.keys.map(keysNode));
    if (vm.phase !== lastPhase) { lastPhase = vm.phase; send({ window: { phase: vm.phase } }); } // tray colour
    if (vm.mode !== lastMode) { lastMode = vm.mode; window.pillHover(-1, -1); send({ window: { mode: vm.mode, side: vm.side } }); }
  }

  window.pillReceive = function (json) {
    var s; try { s = JSON.parse(json); } catch (e) { return; }
    var kind = M.incomingKind(s);
    if (kind === "ignore") return;
    if (kind === "restore") { saveUi({ minimized: false }); return; } // the tray icon was clicked
    if (kind === "dropped") { lastSeen = null; render(); return; }    // backend restarting: say so now
    if (kind === "window") return;
    var wasStale = lastSeen === null || Date.now() - lastSeen > M.STALE_MS;
    var prevVisible = snapshot ? snapshot.visible : null;
    snapshot = s; lastSeen = Date.now();
    var h = M.nextHidden(hidden, prevVisible, s.visible, wasStale);
    if (hidden && !h) lastMode = null; // resend the mode so the wrapper shows the window again
    hidden = h;
    if (ui === null) ui = Object.assign({}, s.ui); // restore once; afterwards the page owns it
    render();
  };

  function cmd(c, extra) { return function () { send(Object.assign({ cmd: c }, extra)); }; }
  $("main").onclick = function () { send({ cmd: M.viewModel(snapshot, lastSeen, Date.now()).main.cmd }); };
  $("pause").onclick = function () { send({ cmd: M.viewModel(snapshot, lastSeen, Date.now()).pause.cmd }); };
  $("dismiss").onclick = function () { send({ cmd: "dismiss" }); hidden = true; send({ window: { mode: "hidden" } }); render(); };
  $("voice").onchange = function () { send({ cmd: "set_voice", voice: this.value }); this.blur(); };
  $("language").onchange = function () { send({ cmd: "set_language", language: this.value }); this.blur(); };
  $("barge").onclick = function () { send({ cmd: "set_barge", on: !(snapshot && snapshot.barge && snapshot.barge.enabled) }); };
  $("beeps").onclick = function () { send({ cmd: "set_beeps", on: !(snapshot && snapshot.beeps) }); };
  $("expand").onclick = function () { if (ui && ui.docked) { saveUi({ dockOpen: true }); return; } saveUi({ expanded: !(ui && ui.expanded) && !(ui && ui.docked), docked: false }); };
  $("minimize").onclick = function () { saveUi({ minimized: true }); };
  $("dock").onclick = $("dockTop").onclick = function () { var d = !(ui && ui.docked); saveUi({ docked: d, expanded: false, tucked: false, dockOpen: false, side: (ui && ui.side) || "right" }); };
  $("drag").onmousedown = $("label").onmousedown = function (e) { if (e.button === 0) send({ window: { drag: true } }); };
  // The wrapper moves the window off the edge; the page swaps to the tab once it has gone.
  $("tuck").onclick = function () {
    if (ui && ui.dockOpen) { saveUi({ dockOpen: false }); return; } // full panel: back to the rail
    send({ window: { slide: "out" } }); setTimeout(function () { saveUi({ tucked: true }); }, 170); };
  $("tab").onclick = function () { saveUi({ tucked: false }); };
  // The wrapper runs the resize loop, as it does for a drag.
  $("edgeL").onmousedown = function (e) { if (e.button === 0) { e.preventDefault(); send({ window: { resize: "left" } }); } };
  $("edgeR").onmousedown = function (e) { if (e.button === 0) { e.preventDefault(); send({ window: { resize: "right" } }); } };
  // Mac: WebKit gives a panel that never takes focus no hover, so the wrapper reports the pointer.
  var hovered = null;
  window.pillHover = function (x, y) {
    var el = x < 0 ? null : document.elementFromPoint(x, y);
    el = el && el.closest(".btn,.tab,.edge");
    if (el === hovered) return;
    if (hovered) hovered.classList.remove("hover");
    if (el) el.classList.add("hover");
    hovered = el;
  };
  setInterval(render, 1000);
  render();
})();
