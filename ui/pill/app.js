// Renders the pill from model.js. Talks to the native wrapper only through
// window.pillTransport.send(json) and window.pillReceive(json).
// Window messages: {"window":{"mode":"pill"|"expanded"|"dot"|"docked"|"hidden","side":...}} and
// {"window":{"drag":true}}. "hidden" is sent once on Dismiss; the wrapper then hides the window.
(function () {
  var M = window.PillModel, $ = function (id) { return document.getElementById(id); };
  var orbButton = $("orbAction"), card = $("card"), orbBubble = orbButton.querySelector(".orb-bubble");
  var lastVisible = false, surfaceVisible = true, levelTimer = null, miniPointer = null;
  var orbPress = null, orbMoved = false;
  var orbPalette = null, lastOrbOnly = null;
  var felixPlayback = null, felixHasSignal = false;
  var bargePending = null, bargeLastShown = null;
  var lastBargeCount = null, interruptedUntil = null;   // the Interrupted flash (model.js interruptFlash)
  var previewAnswer = null;   // the backend's last preview answer, stamped with when it came
  var hidden = false, snapshot = null, lastSeen = null, ui = null, lastMode = null, lastPhase = null, floorPid = null, filled = false;
  var orbPill = window.setupOrbPill(send, saveUi, card, orbButton);

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
    var vm = M.viewModel(snapshot, lastSeen, Date.now(), { interruptedUntil: interruptedUntil });
    var orbEnabled = !!(snapshot && snapshot.orb);
    if (ui) { // local ui wins: it is what the user just chose, and the backend echoes it back
      var mode = M.presentation(ui, orbEnabled);
      vm.mode = mode; vm.side = mode === "docked" || mode === "rail" || mode === "tab" ? (ui.side || "right") : null;
    }
    // Migrate old presentations to the compact/strip surface. On Mac this
    // also recovers a Pill saved in the menu bar before that action was removed.
    if (orbEnabled && (snapshot.platform === "mac" || vm.mode !== "dot")) vm.mode = "pill";
    document.body.dataset.mode = vm.mode;
    document.body.dataset.phase = vm.phase;
    document.body.dataset.tone = vm.indicator.tone;
    document.body.dataset.orb = orbEnabled ? "animated" : "classic";
    if (vm.indicator.tone === "speaking" || vm.indicator.tone === "listening") orbPalette = vm.indicator.tone;
    else if (vm.phase !== "paused") orbPalette = null;
    document.body.dataset.orbPalette = orbPalette || "";
    document.body.dataset.orbMotion = String(orbEnabled && lastVisible && surfaceVisible && !hidden &&
      vm.mode !== "dot" && vm.mode !== "tab" && (vm.indicator.tone === "speaking" || vm.indicator.tone === "listening"));
    var interruptible = vm.phase === "speaking" && snapshot && snapshot.playback_id;
    var orbText = interruptible ? (snapshot.mic_muted ? "Interrupt speech" : "Interrupt speech and listen") : "Toggle voice controls";
    var orbState = [vm.indicator.who, vm.indicator.text].filter(Boolean).join(": ");
    if (snapshot && snapshot.mic_muted) orbState += " — microphone muted";
    orbButton.setAttribute("aria-label", orbText + " — " + orbState);
    orbButton.title = orbText + (orbEnabled && ui && ui.orbDocked === false ? " · Drag to move" : "");
    orbButton.tabIndex = orbEnabled ? 0 : -1;
    orbButton.setAttribute("aria-hidden", String(!orbEnabled));
    $("tray").hidden = !orbEnabled;
    $("keyboard").hidden = !orbEnabled;
    $("tray").textContent = snapshot && snapshot.platform === "mac" ? "Minimize to menu bar" : "Minimize to system tray";
    $("minimize").title = orbEnabled ? "Minimize to mini orb" : "Minimize";
    $("minimize").setAttribute("aria-label", $("minimize").title);
    if (levelTimer !== null) clearTimeout(levelTimer);
    var level = orbEnabled && document.body.dataset.orbMotion === "true" ?
      M.orbLevel(snapshot, vm.phase, lastSeen, Date.now()) : null;
    // Keep the approved still until this reply has actually produced audio.
    if (orbEnabled && vm.phase === "speaking") {
      if (felixPlayback !== snapshot.playback_id) felixHasSignal = false;
      felixPlayback = snapshot.playback_id;
      if (level > 0) felixHasSignal = true;
    } else if (!orbEnabled || vm.phase !== "paused") {
      felixPlayback = null;
      felixHasSignal = false;
    }
    document.body.dataset.felixFrames = String(felixHasSignal);
    orbBubble.style.setProperty("--level", level === null ? 0 : Math.min(1, Math.sqrt(level) * 1.8));
    if (level !== null) levelTimer = setTimeout(function () {
      orbBubble.style.setProperty("--level", 0);
      levelTimer = null;
    }, Math.max(0, 501 - (Date.now() - lastSeen)));
    $("orb").setAttribute("aria-label", [vm.indicator.who, vm.indicator.text].filter(Boolean).join(": "));
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
    // The panel's labelled controls: always in place, words not icons alone (2026-11-17).
    var cp = vm.controls.pause, cm = vm.controls.main;
    $("ctlPause").disabled = !cp.enabled; $("ctlMain").disabled = !cm.enabled;
    $("ctlPauseIcon").textContent = cp.icon; $("ctlPauseText").textContent = vm.controls.pause.text;
    $("ctlPause").setAttribute("aria-label", vm.controls.pause.aria);
    $("ctlPause").setAttribute("aria-pressed", String(vm.controls.pause.pressed));
    $("ctlMainIcon").textContent = cm.kind === "stop" ? "\u25A0\uFE0E" : "";
    $("ctlMainText").textContent = vm.controls.main.text;
    $("ctlMain").setAttribute("aria-label", vm.controls.main.aria); $("ctlMain").title = cm.aria;
    $("ctlMain").classList.toggle("wake", cm.kind === "wake"); $("ctlMain").classList.toggle("stop", cm.kind === "stop");
    $("pause").setAttribute("aria-pressed", String(cp.pressed));
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
    orbPill.update(vm, snapshot, ui, orbEnabled && vm.mode !== "dot", lastVisible && surfaceVisible && !hidden);
    if (orbPill.active) lastMode = null;
    else if (vm.mode !== lastMode) {
      lastMode = vm.mode; window.pillHover(-1, -1);
      send({ window: { mode: vm.mode, side: vm.side } });
    }
    updateOrbRegion();
  }

  window.pillReceive = function (json) {
    var s; try { s = JSON.parse(json); } catch (e) { return; }
    var kind = M.incomingKind(s);
    if (kind === "ignore") return;
    if (kind === "restore") { saveUi({ minimized: false, mini: false }); return; } // the tray icon was clicked
    if (kind === "dropped") { lastSeen = null; render(); return; }    // backend restarting: say so now
    if (kind === "preview") { previewAnswer = Object.assign({ at: Date.now() }, s.window); render(); return; }
    if (kind === "window") return;
    var wasStale = lastSeen === null || Date.now() - lastSeen > M.STALE_MS;
    var prevVisible = snapshot ? snapshot.visible : null;
    snapshot = s; lastSeen = Date.now(); lastVisible = s.visible === true;
    var count = s.barge && typeof s.barge.count === "number" ? s.barge.count : null;
    interruptedUntil = M.interruptFlash(wasStale ? null : lastBargeCount, count, lastSeen, interruptedUntil);
    lastBargeCount = count;
    var h = M.nextHidden(hidden, prevVisible, s.visible, wasStale);
    if (hidden && !h) lastMode = null; // resend the mode so the wrapper shows the window again
    hidden = h;
    if (ui === null) ui = Object.assign({}, s.ui); // restore once; afterwards the page owns it
    if (s.orb && s.platform === "mac" && ui.minimized === true) {
      ui = Object.assign({}, ui, { minimized: false });
      send({ cmd: "save_ui", ui: ui });
    }
    render();
  };

  function cmd(c, extra) { return function () { send(Object.assign({ cmd: c }, extra)); }; }
  $("main").onclick = function () { send({ cmd: M.viewModel(snapshot, lastSeen, Date.now()).main.cmd }); };
  $("pause").onclick = function () { send({ cmd: M.viewModel(snapshot, lastSeen, Date.now()).pause.cmd }); };
  $("ctlPause").onclick = function () { send({ cmd: M.viewModel(snapshot, lastSeen, Date.now()).controls.pause.cmd }); };
  $("ctlMain").onclick = function () { send({ cmd: M.viewModel(snapshot, lastSeen, Date.now()).controls.main.cmd }); };
  $("dismiss").onclick = function () { send({ cmd: "dismiss" }); hidden = true; send({ window: { mode: "hidden" } }); render(); };
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
  $("minimize").onclick = function () {
    if (snapshot && snapshot.orb) saveUi({ mini: true });
    else saveUi({ minimized: true });
  };
  $("tray").onclick = function () { saveUi({ minimized: true }); };
  $("keyboard").onclick = function () { send({ window: { keyboard: true } }); };
  window.pillFocusControls = function () {
    card.dataset.keyboard = "true";
    orbButton.focus();
    orbPill.reveal();
    updateOrbRegion();
  };
  window.addEventListener("blur", function () {
    if (card.dataset.keyboard === "true") {
      delete card.dataset.keyboard;
      orbPill.closeKeyboardHelp();
    }
    orbPill.reveal();
    updateOrbRegion();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && e.target.tagName !== "SELECT" && !e.target.open) {
      delete card.dataset.keyboard;
      document.activeElement.blur();
      orbPill.reveal();
      updateOrbRegion();
      send({ window: { keyboard: false } });
    }
  });
  window.pillOrbDragging = function (moving) {
    if (moving) orbMoved = true;
    card.toggleAttribute("data-dragging", moving);
    orbPill.reveal();
  };
  orbButton.onpointerdown = function (e) {
    miniPointer = null;
    orbPress = null;
    orbMoved = false;
    if (orbPill.active && e.button === 0 && ui && ui.orbDocked === false) {
      orbPress = [e.screenX, e.screenY];
      send({ window: { drag: true, fromOrb: true } });
    } else if (!orbPill.active && e.button === 0 && ui && ui.mini) {
      miniPointer = [e.screenX, e.screenY];
      send({ window: { drag: true } });
    }
  };
  orbButton.onclick = function (e) {
    if (!snapshot || !snapshot.orb) return;
    if (e.detail && orbPress) {
      var travelled = Math.max(Math.abs(e.screenX - orbPress[0]), Math.abs(e.screenY - orbPress[1]));
      orbPress = null;
      if (orbMoved || travelled >= 5) return;
    }
    if (orbPill.orbClick()) return;
    if (ui && ui.mini) {
      var moved = e.detail && miniPointer && Math.hypot(e.screenX - miniPointer[0], e.screenY - miniPointer[1]) > 4;
      miniPointer = null;
      if (!moved) saveUi({ mini: false });
      return;
    }
    var vm = M.viewModel(snapshot, lastSeen, Date.now());
    if (vm.phase === "speaking" && snapshot.playback_id) {
      send({ cmd: "interrupt_playback", playback_id: snapshot.playback_id });
      return;
    }
    $("expand").click();
  };
  $("dock").onclick = $("dockTop").onclick = function () { var d = !(ui && ui.docked); saveUi({ docked: d, expanded: false, tucked: false, dockOpen: false, side: (ui && ui.side) || "right" }); };
  $("drag").onmousedown = $("label").onmousedown = function (e) { if (e.button === 0 && !(snapshot && snapshot.orb && e.target.closest(".orb-action"))) send({ window: { drag: true } }); };
  // The wrapper moves the window off the edge; the page swaps to the tab once it has gone.
  $("tuck").onclick = function () {
    if (ui && ui.dockOpen) { saveUi({ dockOpen: false }); return; } // full panel: back to the rail
    send({ window: { slide: "out" } }); setTimeout(function () { saveUi({ tucked: true }); }, 170); };
  $("tab").onclick = function () { saveUi({ tucked: false }); };
  // The wrapper runs the resize loop, as it does for a drag.
  $("edgeL").onmousedown = function (e) { if (e.button === 0) { e.preventDefault(); send({ window: { resize: "left" } }); } };
  $("edgeR").onmousedown = function (e) { if (e.button === 0) { e.preventDefault(); send({ window: { resize: "right" } }); } };
  function updateOrbRegion() {
    // In Windows the native window is opaque. Clip it to the orb until controls appear.
    if (!document.documentElement.classList.contains("windows") || orbPill.active) return;
    var only = document.body.dataset.conceal === "true" && !card.classList.contains("pointer-in") &&
      card.dataset.keyboard !== "true" && !card.querySelector(":focus-visible");
    if (only === lastOrbOnly) return;
    lastOrbOnly = only;
    var rect = orbButton.getBoundingClientRect();
    send({ window: { orbRegion: only ? [Math.round(rect.x) - 3, Math.round(rect.y) - 3,
      Math.round(rect.width) + 6, Math.round(rect.height) + 6] : null } });
  }

  // Mac: WebKit gives a panel that never takes focus no hover, so the wrapper reports the pointer.
  var hovered = null;
  window.pillHover = function (x, y) {
    var el = x < 0 ? null : document.elementFromPoint(x, y);
    orbPill.point(el);
    card.classList.toggle("pointer-in", !!(el && (el.closest(".orb-action") ||
      (card.classList.contains("pointer-in") && card.contains(el)))));
    el = el && el.closest(".btn,.tab,.edge");
    if (el === hovered) return;
    if (hovered) hovered.classList.remove("hover");
    if (el) el.classList.add("hover");
    hovered = el;
  };
  card.addEventListener("pointerover", function (e) {
    orbPill.point(e.target);
    if (e.target.closest(".orb-action")) { card.classList.add("pointer-in"); updateOrbRegion(); }
  });
  card.addEventListener("pointerleave", function () {
    card.classList.remove("pointer-in"); orbPill.point(null); updateOrbRegion();
  });
  card.addEventListener("focusin", function () { orbPill.reveal(); updateOrbRegion(); });
  card.addEventListener("focusout", function () { setTimeout(function () { orbPill.reveal(); updateOrbRegion(); }, 0); });
  window.pillSurfaceVisible = function (visible) { surfaceVisible = visible; render(); };
  document.addEventListener("visibilitychange", function () {
    surfaceVisible = !document.hidden;
    render();
  });
  setInterval(render, 1000);
  render();
})();
