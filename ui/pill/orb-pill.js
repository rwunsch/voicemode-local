/* Spectrum surface for the orb. Audio and session state still belong to app.js/the backend. */
(function () {
  var INTRO_MS = 4800, EXIT_MS = 185;
  window.setupOrbPill = function (send, save, card, orb) {
    var $ = function (id) { return document.getElementById(id); };
    var body = document.body, theme = $("pillTheme");
    var lang = $("orbLanguage"), voice = $("orbVoice"), mute = $("orbMute"), end = $("orbEnd");
    var menu = $("orbMenu"), settings = $("orbSettings"), strip = $("orbStrip");
    var tray = $("orbTray");
    var panel = "", active = false, opened = false, hover = false, intro = false, pinned = false;
    var introTimer, closeTimer, lastFrame = "", mutePending, muted, vm, snapshot, ui;
    var appearance = matchMedia("(prefers-color-scheme: dark)");
    function syncTheme() { theme.setAttribute("color", appearance.matches ? "dark" : "light"); }
    syncTheme();
    appearance.addEventListener("change", syncTheme);

    function layout(surface) {
      var side = ui && ui.side === "left" ? "left" : "right";
      var mode = ui && ui.orbDocked === false ? "orb-floating" : "orb-docked";
      var key = mode + ":" + side + ":" + surface;
      body.dataset.orbSide = side;
      if (key === lastFrame) return;
      lastFrame = key;
      send({ window: { mode: mode, side: side, surface: surface } });
    }
    function showRegion() {
      if (!active) return;
      var keyFocus = card.dataset.keyboard === "true" || !!card.querySelector(":focus-visible");
      var visible = !!(intro || hover || panel || pinned || keyFocus);
      var was = body.dataset.orbReveal === "true";
      var first = !lastFrame;
      body.dataset.orbReveal = String(visible);
      orb.setAttribute("aria-expanded", String(visible));
      $("orbMore").setAttribute("aria-expanded", String(panel === "menu"));
      strip.inert = !visible;
      menu.hidden = panel !== "menu";
      settings.hidden = panel !== "settings";
      if (visible || first) {
        clearTimeout(closeTimer);
        layout(visible ? panel || "strip" : "compact");
      } else if (was) {
        clearTimeout(closeTimer);
        closeTimer = setTimeout(function () {
          if (active && body.dataset.orbReveal === "false") layout("compact");
        }, EXIT_MS);
      }
    }
    function finishIntro() { clearTimeout(introTimer); intro = false; }
    function beginIntro() {
      finishIntro();
      intro = true;
      introTimer = setTimeout(function () { intro = false; showRegion(); }, INTRO_MS);
    }
    function backToOrb() {
      panel = "";
      if (card.dataset.keyboard === "true") $("orbMore").focus();
      showRegion();
    }
    function setText(el, value) { if (el.textContent !== value) el.textContent = value; }
    function picker(el, values, value) {
      var key = values.join("\u0000");
      if (el.dataset.values !== key) {
        el.dataset.values = key;
        el.replaceChildren.apply(el, values.map(function (name) {
          var item = document.createElement("sp-menu-item");
          item.setAttribute("value", name);
          item.textContent = name;
          return item;
        }));
      }
      if (!el.open && el.value !== (value || "")) el.value = value || "";
    }
    function update(model, data, state, isActive, isShown) {
      vm = model; snapshot = data; ui = state;
      if (!isActive) {
        if (active) {
          delete body.dataset.orbLayout;
          delete body.dataset.orbReveal;
          delete body.dataset.orbAbove;
          orb.removeAttribute("aria-expanded");
        }
        active = opened = pinned = hover = false; panel = ""; lastFrame = "";
        menu.hidden = settings.hidden = true;
        finishIntro(); clearTimeout(closeTimer);
        return;
      }
      active = true;
      body.dataset.orbLayout = "";
      if (isShown && !opened) beginIntro();
      if (!isShown && opened) { finishIntro(); pinned = hover = false; panel = ""; lastFrame = ""; }
      opened = isShown;
      muted = !!(data && data.mic_muted);
      body.dataset.micMuted = String(muted);
      if (mutePending && (mutePending.on === muted || Date.now() >= mutePending.until || !vm.connected)) mutePending = null;
      setText(mute, muted ? "Unmute mic" : "Mute mic");
      mute.toggleAttribute("selected", muted);
      mute.disabled = !vm.connected || typeof data.mic_muted !== "boolean" || !!mutePending;
      setText(end, vm.controls.main.kind === "wake" ? "Start voice" : "End voice");
      end.disabled = !vm.controls.main.enabled;
      setText($("orbPause"), vm.controls.pause.pressed ? "Resume voice" : "Pause voice");
      $("orbPause").disabled = !vm.controls.pause.enabled;
      setText($("orbDock"), ui && ui.orbDocked === false ? "Dock to screen edge" : "Undock");
      tray.hidden = data.platform === "mac";
      if (panel === "settings") {
        picker(lang, vm.languages, vm.language);
        picker(voice, vm.voices, vm.voice);
        lang.disabled = voice.disabled = !vm.pickersEnabled;
        $("orbPreview").disabled = $("preview").disabled;
        $("orbPreview").setAttribute("aria-busy", $("preview").getAttribute("aria-busy") || "false");
        $("orbPreview").title = $("preview").title;
        setText($("orbPreviewStatus"), $("previewStatus").textContent);
        $("orbBarge").checked = $("barge").getAttribute("aria-checked") === "true";
        $("orbBarge").disabled = !vm.barge.enabled;
        $("orbBarge").hidden = !vm.barge.visible;
        $("orbBeeps").checked = vm.beeps.on;
        $("orbBeeps").disabled = !vm.connected;
      }
      showRegion();
    }
    function point(el) {
      if (!active) return;
      var inside = !!(el && (el === orb || orb.contains(el) ||
        (body.dataset.orbReveal === "true" && card.contains(el))));
      if (hover && !inside) { finishIntro(); pinned = false; }
      if (hover !== inside) { hover = inside; showRegion(); }
    }
    function openedPanel(next) {
      panel = panel === next ? "" : next;
      update(vm, snapshot, ui, active, opened);
      if (panel === "settings" && card.dataset.keyboard === "true") $("orbSettingsDone").focus();
      if (panel === "menu" && card.dataset.keyboard === "true") $("orbOpenSettings").focus();
    }
    window.pillOrbAbove = function (above) { body.dataset.orbAbove = String(above); };
    mute.addEventListener("click", function () {
      if (mute.disabled) return;
      mutePending = { on: !muted, until: Date.now() + 2200 };
      mute.disabled = true;
      send({ cmd: "set_mic_muted", on: mutePending.on });
    });
    end.addEventListener("click", function () {
      if (end.disabled) return;
      if (vm.controls.main.kind === "wake") send({ cmd: vm.controls.main.cmd });
      else if (snapshot.floor) send({ cmd: "end_voice", pid: snapshot.floor.pid });
      finishIntro(); pinned = false; panel = "";
      showRegion();
    });
    $("orbMore").addEventListener("click", function () { openedPanel("menu"); });
    $("orbOpenSettings").addEventListener("click", function () { openedPanel("settings"); });
    $("orbSettingsDone").addEventListener("click", backToOrb);
    $("orbPause").addEventListener("click", function () { $("ctlPause").click(); backToOrb(); });
    $("orbDock").addEventListener("click", function () {
      panel = ""; save({ orbDocked: ui && ui.orbDocked === false });
    });
    tray.addEventListener("click", function () {
      if (!tray.hidden) { panel = ""; save({ minimized: true }); }
    });
    $("orbKeyboard").addEventListener("click", function () {
      send({ window: { keyboard: true } });
      showRegion();
    });
    $("orbPreview").addEventListener("click", function () { $("preview").click(); });
    $("orbBarge").addEventListener("change", function () { $("barge").click(); });
    $("orbBeeps").addEventListener("change", function () { $("beeps").click(); });
    [[lang, $("language")], [voice, $("voice")]].forEach(function (pair) {
      pair[0].addEventListener("change", function () {
        pair[1].value = pair[0].value;
        pair[1].onchange();
      });
    });
    card.addEventListener("keydown", function (e) {
      if (!active || e.key !== "Escape" || !panel || voice.open || lang.open) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      backToOrb();
    });
    return {
      update: update, point: point, reveal: showRegion,
      orbClick: function () {
        if (!active) return false;
        if (panel) backToOrb();
        else if (vm.phase === "speaking" && snapshot.playback_id) {
          send({ cmd: "interrupt_playback", playback_id: snapshot.playback_id });
        } else { pinned = !pinned; showRegion(); }
        return true;
      },
      get active() { return active; },
    };
  };
})();
