// Renders the pill from model.js. Talks to the native wrapper only through
// window.pillTransport.send(json) and window.pillReceive(json).
// Window messages: {"window":{"mode":"pill"|"expanded"|"dot"|"docked"|"hidden","side":...}} and
// {"window":{"drag":true}}. "hidden" is sent once on Dismiss; the wrapper then hides the window.
(function () {
  var M = window.PillModel, $ = function (id) { return document.getElementById(id); };
  var bargePending = null, bargeLastShown = null;
  var lastBargeCount = null, interruptedUntil = null;   // the Interrupted flash (model.js interruptFlash)
  var previewAnswer = null;   // the backend's last preview answer, stamped with when it came
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

  var changedAt = {}, shownKind = {}, hovered = null;
  function setPanelOnly(el, b) {
    el.hidden = !b.visible; el.disabled = !b.enabled;
    el.dataset.hint = b.hint || ""; el.title = b.hint || ""; el.setAttribute("aria-label", b.hint || b.text || "");
  }
  function setSpot(icon, ctl, ctlIcon, ctlText, b) {
    var which = icon.id === "main" ? "primary" : "end";
    if (shownKind[which] !== b.kind) { shownKind[which] = b.kind; changedAt[which] = Date.now(); }
    [icon, ctl].forEach(function (el) {
      el.hidden = !b.visible; el.disabled = !b.enabled;
      el.dataset.kind = b.kind || "";
      el.dataset.hint = b.hint || "";
      el.title = b.title || b.hint || b.text || "";  // native tooltip where there is one (Windows, Linux)
      el.setAttribute("aria-label", b.hint || b.text || "");
      if (b.toggle) el.setAttribute("aria-pressed", String(!!b.pressed)); else el.removeAttribute("aria-pressed");
    });
    if (icon.id === "pause") icon.textContent = b.icon;
    else { // the left button: a glyph, or the sunrise for Wake and Resume voice
      $("mainStop").textContent = b.icon || ""; $("mainStop").hidden = !b.icon;
      $("mainWake").hidden = !!b.icon || !b.visible;
    }
    ctlIcon.textContent = b.icon || ""; ctlText.textContent = b.text || "";
  }
  // Hover hints in the status line: a Mac panel that never takes focus shows no native
  // tooltip, and a tooltip would be cut off by the small window. The Mac wrapper reports the
  // pointer (pillHover, below); elsewhere mouseover does the same.
  function hoverHint() {
    var el = hovered && hovered.closest ? hovered.closest(".btn") : null;
    return el && !el.hidden && el.dataset && el.dataset.hint ? el.dataset.hint : "";
  }
  function render() {
    var vm = M.viewModel(snapshot, lastSeen, Date.now(), { interruptedUntil: interruptedUntil });
    if (ui) { // local ui wins: it is what the user just chose, and the backend echoes it back
      var mode = ui.minimized ? "dot" : ui.docked ? (ui.tucked ? "tab" : ui.dockOpen ? "docked" : "rail") : ui.expanded ? "expanded" : "pill";
      vm.mode = mode; vm.side = mode === "docked" || mode === "rail" || mode === "tab" ? (ui.side || "right") : null;
    }
    document.body.dataset.mode = vm.mode;
    document.body.dataset.phase = vm.phase;
    document.body.dataset.tone = vm.indicator.tone;
    $("orb").setAttribute("aria-label", [vm.indicator.who, vm.indicator.text].filter(Boolean).join(": "));
    $("label").textContent = hoverHint() || vm.stateText;
    $("waiting").textContent = vm.waitingText;
    $("sub").textContent = vm.session || "Voice pill";
    var pid = snapshot && snapshot.floor ? snapshot.floor.pid : null;
    if (pid !== floorPid) { // a focused select is not refreshed: let go when the floor moves
      floorPid = pid;
      var f = document.activeElement;
      if (f === $("voice") || f === $("language")) document.activeElement.blur();
    }
    document.body.hidden = hidden;
    [["voice", vm.pickersEnabled], ["language", vm.pickersEnabled], ["barge", vm.barge.enabled],
      ["minimize", vm.minimizeEnabled], ["dismiss", vm.dismissEnabled], ["expand", true], ["dock", true]]
      .forEach(function (p) { $(p[0]).disabled = !p[1]; });
    // Two spots in a fixed place (FELIX #2094, UX review 2026-11-27): left = primary (Pause,
    // Done, Resume, Wake, Resume voice), right = Voice off. The compact row shows icons; the
    // panel shows the same two buttons with words (CSS hides the icons when the panel is open).
    // The element ids are older than this layout: #main and #ctlPause are the left spot,
    // #pause and #ctlMain the right one.
    setSpot($("main"), $("ctlPause"), $("ctlPauseIcon"), $("ctlPauseText"), vm.primary);
    setSpot($("pause"), $("ctlMain"), $("ctlMainIcon"), $("ctlMainText"), vm.end);
    setPanelOnly($("ctlMic"), vm.mic);   // Pause mic while listening, panel only
    $("dismiss").dataset.hint = vm.close.hint; $("dismiss").title = vm.close.hint;
    $("dismiss").setAttribute("aria-label", vm.close.hint);
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
    var pv = M.previewView(previewAnswer, Date.now(), vm.connected, vm.inTurn);
    $("preview").disabled = !pv.enabled || !$("voice").value;
    $("preview").title = pv.title;
    $("preview").setAttribute("aria-busy", String(pv.pending));
    $("previewStatus").textContent = pv.text;
    $("bargeRow").style.display = vm.barge.visible ? "" : "none";
    var bs = M.bargeShown(vm.barge.on, bargePending, Date.now());
    bargePending = bs.keep;
    $("barge").textContent = bs.on ? "On" : "Off";
    $("barge").setAttribute("aria-checked", String(bs.on));
    $("barge").setAttribute("aria-busy", String(bs.pending));
    $("barge").classList.toggle("pending", bs.pending);
    if (bs.on !== bargeLastShown) { // the window log shows when the switch changed on screen
      bargeLastShown = bs.on;
      send({ window: { log: "barge shown " + (bs.on ? "on" : "off") + (bs.pending ? " (pending)" : "") } });
    }
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
    if (kind === "quit") { quitPill(); return; }                       // "Quit pill" in the tray menu
    if (kind === "dropped") { lastSeen = null; render(); return; }    // backend restarting: say so now
    if (kind === "preview") { previewAnswer = Object.assign({ at: Date.now() }, s.window); render(); return; }
    if (kind === "window") return;
    var wasStale = lastSeen === null || Date.now() - lastSeen > M.STALE_MS;
    var prevVisible = snapshot ? snapshot.visible : null;
    snapshot = s; lastSeen = Date.now();
    var count = s.barge && typeof s.barge.count === "number" ? s.barge.count : null;
    interruptedUntil = M.interruptFlash(wasStale ? null : lastBargeCount, count, lastSeen, interruptedUntil);
    lastBargeCount = count;
    var h = M.nextHidden(hidden, prevVisible, s.visible, wasStale);
    if (hidden && !h) lastMode = null; // resend the mode so the wrapper shows the window again
    hidden = h;
    if (ui === null) ui = Object.assign({}, s.ui); // restore once; afterwards the page owns it
    render();
  };

  function cmd(c, extra) { return function () { send(Object.assign({ cmd: c }, extra)); }; }
  // A click lands on what the button showed when it was aimed at: a button whose meaning
  // changed in the last GUARD_MS (a reply ending turns Pause into Done) ignores the click.
  function press(which) {
    return function () {
      var b = M.viewModel(snapshot, lastSeen, Date.now())[which];
      if (!b.visible || !b.enabled || !b.cmd) return;
      if (Date.now() - (changedAt[which] || 0) < M.GUARD_MS) return;
      send({ cmd: b.cmd });
    };
  }
  $("main").onclick = press("primary"); $("ctlPause").onclick = press("primary");
  $("pause").onclick = press("end"); $("ctlMain").onclick = press("end");
  $("ctlMic").onclick = press("mic");
  // Close shrinks to the tray / menu bar (Robert 2026-11-27); quitting the pill is in that
  // icon's menu, whose wrapper sends {"cmd":"quit"}: until the next voice session, as before.
  function quitPill() { send({ cmd: "dismiss" }); hidden = true; send({ window: { mode: "hidden" } }); render(); }
  $("dismiss").onclick = function () { saveUi({ minimized: true }); };
  // Hear the voice shown in the list; one at a time (the button waits for the answer).
  $("preview").onclick = function () {
    if (!$("voice").value) return;
    previewAnswer = { preview: "loading", voice: $("voice").value, at: Date.now() };
    send({ cmd: "preview_voice", voice: $("voice").value });
    render();
  };
  $("voice").onchange = function () { send({ cmd: "set_voice", voice: this.value }); this.blur(); };
  $("language").onchange = function () {
    // A voice speaks its own language: switch to that language's voice first (the current one if
    // it is listed there, else the first), then the language. Nothing listed: the language alone.
    var voice = M.voiceForLanguage(snapshot && snapshot.voices_by_language, this.value, snapshot && snapshot.voice);
    if (voice && voice !== (snapshot && snapshot.voice)) send({ cmd: "set_voice", voice: voice });
    send({ cmd: "set_language", language: this.value });
    this.blur();
  };
  $("barge").onclick = function () {
    bargePending = M.clickBarge(snapshot && snapshot.barge && snapshot.barge.enabled, bargePending, Date.now());
    send({ window: { log: "click barge " + (bargePending.on ? "on" : "off") } });
    send({ cmd: "set_barge", on: bargePending.on });
    render();
  };
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
  function setHovered(el) {
    if (el === hovered) return;
    if (hovered) hovered.classList.remove("hover");
    if (el) el.classList.add("hover");
    hovered = el;
    $("label").textContent = hoverHint() || M.viewModel(snapshot, lastSeen, Date.now(),
      { interruptedUntil: interruptedUntil }).stateText;
  }
  window.pillHover = function (x, y) {
    var el = x < 0 ? null : document.elementFromPoint(x, y);
    setHovered(el && el.closest(".btn,.tab,.edge"));
  };
  document.addEventListener("mouseover", function (e) {
    setHovered(e.target && e.target.closest ? e.target.closest(".btn,.tab,.edge") : null);
  });
  document.addEventListener("mouseleave", function () { setHovered(null); });
  setInterval(render, 1000);
  render();
})();
