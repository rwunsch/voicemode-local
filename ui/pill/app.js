// Renders the pill from model.js. Talks to the native wrapper only through
// window.pillTransport.send(json) and window.pillReceive(json).
(function () {
  var M = window.PillModel, $ = function (id) { return document.getElementById(id); };
  var hidden = false, snapshot = null, lastSeen = null, ui = null, lastMode = null, filled = false;

  function send(obj) {
    try { if (window.pillTransport) window.pillTransport.send(JSON.stringify(obj)); } catch (e) { /* no transport */ }
  }
  function saveUi(patch) {
    ui = Object.assign({}, ui, patch);
    send({ cmd: "save_ui", ui: ui });
    render();
  }
  function fill(select, items, current) {
    if (!filled) items.forEach(function (v) { var o = document.createElement("option"); o.value = o.textContent = v; select.appendChild(o); });
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
      var mode = ui.minimized ? "dot" : ui.docked ? "docked" : ui.expanded ? "expanded" : "pill";
      vm.mode = mode; vm.side = mode === "docked" ? (ui.side || "right") : null;
    }
    document.body.dataset.mode = vm.mode;
    document.body.dataset.phase = vm.phase;
    $("label").textContent = vm.label;
    $("sub").textContent = vm.connected && snapshot.floor ? (snapshot.floor.label || "") : "";
    document.body.hidden = hidden;
    [["interrupt", vm.interrupt.enabled], ["stop", vm.stop.enabled], ["pause", vm.pause.enabled],
      ["voice", vm.pickersEnabled], ["language", vm.pickersEnabled], ["barge", vm.barge.enabled],
      ["minimize", vm.minimizeEnabled], ["dismiss", vm.dismissEnabled], ["expand", true], ["dock", true]]
      .forEach(function (p) { $(p[0]).disabled = !p[1]; });
    $("pause").textContent = vm.pause.icon;
    $("pause").title = vm.pause.label; $("pause").setAttribute("aria-label", vm.pause.label);
    $("expand").setAttribute("aria-expanded", String(vm.mode === "expanded" || vm.mode === "docked"));
    $("dock").textContent = vm.mode === "docked" ? "Undock" : "Dock";
    fill($("voice"), vm.voices, vm.voice); fill($("language"), vm.languages, vm.language); filled = true;
    $("bargeRow").style.display = vm.barge.visible ? "" : "none";
    $("barge").textContent = vm.barge.on ? "On" : "Off";
    $("barge").setAttribute("aria-checked", String(vm.barge.on));
    $("count").textContent = vm.barge.visible ? vm.barge.countText : "";
    var q = $("queue"); q.replaceChildren();
    vm.queue.forEach(function (r) {
      var d = document.createElement("div"); d.className = "q";
      var a = document.createElement("span"), b = document.createElement("span");
      a.textContent = r.name; b.textContent = r.text; b.className = "m"; d.append(a, b); q.appendChild(d);
    });
    $("keys").replaceChildren.apply($("keys"), vm.keys.map(keysNode));
    if (vm.mode !== lastMode) { lastMode = vm.mode; send({ window: { mode: vm.mode, side: vm.side } }); }
  }

  window.pillReceive = function (json) {
    var s; try { s = JSON.parse(json); } catch (e) { return; }
    if (!s || typeof s !== "object") return;
    snapshot = s; lastSeen = Date.now();
    if (hidden && s.visible === false) { hidden = false; lastMode = null; } // a later new session shows it again
    if (ui === null) ui = Object.assign({}, s.ui); // restore once; afterwards the page owns it
    render();
  };

  function cmd(c, extra) { return function () { send(Object.assign({ cmd: c }, extra)); }; }
  $("interrupt").onclick = cmd("interrupt");
  $("stop").onclick = cmd("stop");
  $("pause").onclick = function () { send({ cmd: M.viewModel(snapshot, lastSeen, Date.now()).pause.cmd }); };
  $("dismiss").onclick = function () { send({ cmd: "dismiss" }); hidden = true; send({ window: { mode: "hidden" } }); render(); };
  $("voice").onchange = function () { send({ cmd: "set_voice", voice: this.value }); };
  $("language").onchange = function () { send({ cmd: "set_language", language: this.value }); };
  $("barge").onclick = function () { send({ cmd: "set_barge", on: !(snapshot && snapshot.barge && snapshot.barge.enabled) }); };
  $("expand").onclick = function () { saveUi({ expanded: !(ui && ui.expanded) && !(ui && ui.docked), docked: false }); };
  $("minimize").onclick = function () { saveUi({ minimized: true }); };
  $("dot").onclick = function () { saveUi({ minimized: false }); };
  $("dock").onclick = function () { var d = !(ui && ui.docked); saveUi({ docked: d, expanded: false, side: (ui && ui.side) || "right" }); };
  $("drag").onmousedown = function (e) { if (e.button === 0) send({ window: { drag: true } }); };
  setInterval(render, 1000);
  render();
})();
