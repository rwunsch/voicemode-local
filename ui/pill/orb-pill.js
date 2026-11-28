/* Spectrum surface for the orb. Audio and session state still belong to app.js/the backend. */
(function () {
  var INTRO_MS = 4800, EXIT_MS = 185;
  window.setupOrbPill = function (send, save, card, orb) {
    var $ = function (id) { return document.getElementById(id); };
    var body = document.body, theme = $("pillTheme");
    var lang = $("orbLanguage"), voice = $("orbVoice"), mute = $("orbMute"), end = $("orbEnd");
    var menu = $("orbMenu"), settings = $("orbSettings"), strip = $("orbStrip"), keys = $("orbKeyboardHelp");
    var tray = $("orbTray"), pause = $("orbPause"), gear = $("orbSettingsToggle");
    var panel = "", active = false, opened = false, hover = false, intro = false, pinned = false;
    var introTimer, closeTimer, lastFrame = "", mutePending, muted, vm, snapshot, ui, watchingMenu = false;
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
    function watchMenu(open) {
      if (watchingMenu === open) return;
      watchingMenu = open;
      send({ window: { orbMenuOpen: open } });
    }
    function showRegion() {
      if (!active) return;
      var keyFocus = card.dataset.keyboard === "true" || !!card.querySelector(":focus-visible");
      var visible = !!(intro || hover || panel || pinned || keyFocus || card.hasAttribute("data-dragging"));
      var was = body.dataset.orbReveal === "true";
      var first = !lastFrame;
      body.dataset.orbReveal = String(visible);
      orb.setAttribute("aria-expanded", String(visible));
      gear.setAttribute("aria-expanded", String(panel === "menu"));
      strip.inert = !visible;
      menu.hidden = panel !== "menu";
      settings.hidden = panel !== "settings";
      keys.hidden = panel !== "keyboard";
      if (visible || first) {
        clearTimeout(closeTimer);
        layout(visible ? (panel === "keyboard" ? "menu" : panel || "strip") : "compact");
      } else if (was) {
        clearTimeout(closeTimer);
        closeTimer = setTimeout(function () {
          if (active && body.dataset.orbReveal === "false") layout("compact");
        }, EXIT_MS);
      }
      watchMenu(opened && panel === "menu");
    }
    function finishIntro() { clearTimeout(introTimer); intro = false; }
    function beginIntro() {
      finishIntro();
      intro = true;
      introTimer = setTimeout(function () { intro = false; showRegion(); }, INTRO_MS);
    }
    function backToOrb() {
      panel = "";
      if (card.dataset.keyboard === "true") gear.focus();
      showRegion();
    }
    function closeKeyboardHelp() {
      if (panel === "keyboard") panel = "";
      showRegion();
    }
    function releaseKeyboard() {
      delete card.dataset.keyboard;
      if (card.contains(document.activeElement)) document.activeElement.blur();
      closeKeyboardHelp();
      send({ window: { keyboard: false } });
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
          delete body.dataset.orbFloating;
          orb.removeAttribute("aria-expanded");
        }
        active = opened = pinned = hover = false; panel = ""; lastFrame = "";
        menu.hidden = settings.hidden = keys.hidden = true;
        watchMenu(false);
        finishIntro(); clearTimeout(closeTimer);
        return;
      }
      active = true;
      body.dataset.orbLayout = "";
      body.toggleAttribute("data-orb-floating", !!(ui && ui.orbDocked === false));
      if (isShown && !opened) beginIntro();
      if (!isShown && opened) { finishIntro(); pinned = hover = false; panel = ""; lastFrame = ""; }
      opened = isShown;
      muted = !!(data && data.mic_muted);
      body.dataset.micMuted = String(muted);
      if (mutePending && (mutePending.on === muted || Date.now() >= mutePending.until || !vm.connected)) mutePending = null;
      setText(mute, muted ? "Unmute mic" : "Mute mic");
      mute.toggleAttribute("selected", muted);
      mute.disabled = !vm.connected || typeof data.mic_muted !== "boolean" || !!mutePending;
      var actions = vm.orbControls;
      var wake = actions.end.kind === "wake", paused = actions.pause.pressed;
      setText(end, wake ? "Start voice" : "End voice");
      end.variant = wake ? "accent" : "negative";
      end.disabled = !actions.end.enabled;
      setText(pause, paused ? "Resume" : "Pause");
      pause.setAttribute("aria-label", paused ? "Resume voice" : "Pause voice");
      pause.toggleAttribute("selected", paused);
      pause.disabled = !actions.pause.enabled;
      setText($("orbDock"), ui && ui.orbDocked === false ? "Redock" : "Undock");
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
    window.pillOrbDismissMenu = function () {
      if (panel !== "menu") return;
      panel = "";
      hover = pinned = false;
      finishIntro();
      if (card.dataset.keyboard === "true") releaseKeyboard(); else showRegion();
    };
    document.addEventListener("click", function (e) {
      if (panel !== "menu" || !e.detail) return;
      var path = e.composedPath();
      if (path.includes(menu) || path.includes(gear)) return;
      // The first click around an open menu dismisses it, even over End voice.
      e.preventDefault();
      e.stopImmediatePropagation();
      backToOrb();
    }, true);
    mute.addEventListener("click", function () {
      if (mute.disabled) return;
      mutePending = { on: !muted, until: Date.now() + 2200 };
      mute.disabled = true;
      send({ cmd: "set_mic_muted", on: mutePending.on });
    });
    end.addEventListener("click", function () {
      if (end.disabled) return;
      var cmd = vm.orbControls.end.cmd;
      if (cmd === "wake") send({ cmd: cmd });
      else if (cmd === "end_voice" && snapshot.floor) send({ cmd: cmd, pid: snapshot.floor.pid });
      finishIntro(); pinned = false; panel = "";
      showRegion();
    });
    gear.addEventListener("click", function () { openedPanel("menu"); });
    $("orbOpenSettings").addEventListener("click", function () { openedPanel("settings"); });
    $("orbSettingsDone").addEventListener("click", backToOrb);
    pause.addEventListener("click", function () { if (!pause.disabled) send({ cmd: vm.orbControls.pause.cmd }); });
    $("orbDock").addEventListener("click", function () {
      panel = ""; save({ orbDocked: ui && ui.orbDocked === false });
    });
    window.pillOrbDocked = function (side) {
      if (!active || !ui || ui.orbDocked !== false || (side !== "left" && side !== "right")) return;
      panel = "";
      save({ orbDocked: true, side: side });
    };
    tray.addEventListener("click", function () {
      if (!tray.hidden) { panel = ""; save({ minimized: true }); }
    });
    $("orbKeyboard").addEventListener("click", function () {
      openedPanel("keyboard");
      send({ window: { keyboard: true } });
    });
    $("orbKeyboardDone").addEventListener("click", releaseKeyboard);
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
      if (panel === "keyboard") releaseKeyboard(); else backToOrb();
    });
    return {
      update: update, point: point, reveal: showRegion, closeKeyboardHelp: closeKeyboardHelp,
      orbClick: function () {
        if (!active) return false;
        if (panel === "keyboard") releaseKeyboard();
        else if (panel) backToOrb();
        else if (vm.phase === "speaking" && snapshot.playback_id) {
          send({ cmd: "interrupt_playback", playback_id: snapshot.playback_id });
        } else { pinned = !pinned; showRegion(); }
        return true;
      },
      get active() { return active; },
    };
  };
})();
