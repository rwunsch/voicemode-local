"""ui/pill markup: every element app.js shows or hides through `.hidden` must be an HTML
element. SVGElement has no `hidden` property, so `svg.hidden = false` only sets a JS
field and the `hidden` attribute stays: the Wake sunrise never showed on a Mac
(2026-11-07, n=1: "it's an empty button")."""
import re
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "ui" / "pill"


def test_elements_toggled_with_hidden_are_html_elements():
    js = (UI / "app.js").read_text(encoding="utf-8")
    html = (UI / "index.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)\.hidden\s*=', js))
    assert ids, "app.js toggles nothing through .hidden?"
    for el_id in ids:
        m = re.search(r'<(\w+)[^>]*\bid="%s"' % re.escape(el_id), html)
        assert m, f"{el_id} not in index.html"
        assert m.group(1).lower() not in ("svg", "path", "g", "use"), \
            f"#{el_id} is <{m.group(1)}>: SVG has no .hidden; wrap it in a span"


def test_the_wake_button_holds_the_sunrise():
    html = (UI / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<span id="mainWake"[^>]*>\s*<svg[^>]*class="sun"', html)
    assert m, "#mainWake must be a span wrapping the sunrise svg"


def test_both_windows_log_each_hop_of_a_click():
    """Mac, 2026-11-13: a late or dropped barge click could not be told apart from the logs.
    The page traces the click and the switch changing on screen; both wrappers write those lines
    and each command they hand to the backend, so a repeat shows which hop failed."""
    root = Path(__file__).resolve().parent.parent
    app = (root / "ui" / "pill" / "app.js").read_text(encoding="utf-8")
    assert '"click barge "' in app and '"barge shown "' in app
    ps1 = (root / "pill-windows.ps1").read_text(encoding="utf-8")
    assert 'w.TryGetValue("log", out v)' in ps1 and '"cmd -> backend: "' in ps1
    mac = (root / "patches" / "voice_pill_mac.py").read_text(encoding="utf-8")
    assert '"page: "' in mac and "cmd -> backend:" in mac


# -- 2026-11-17, a tester's feedback (n=1): controls easy to find, a coloured indicator ------

def _tag(html, el_id):
    m = re.search(r'<(\w+)([^>]*)\bid="%s"([^>]*)>(.*?)</\1>' % re.escape(el_id), html, re.S)
    assert m, f"#{el_id} not in index.html"
    return m.group(1).lower(), m.group(2) + m.group(3), m.group(4)


def test_the_panel_has_labelled_pause_and_stop_buttons():
    html = (UI / "index.html").read_text(encoding="utf-8")
    tag, attrs, _ = _tag(html, "controls")
    assert 'role="group"' in attrs and 'aria-label="Voice controls"' in attrs
    # first thing in the expanded panel, so it is found before the settings
    assert html.index('id="more"') < html.index('id="controls"') < html.index('id="voice"')
    for el_id, text, aria in (("ctlPause", "Pause", "Pause the voice turn"),
                              ("ctlMain", "Stop", "Stop the voice turn")):
        tag, attrs, inner = _tag(html, el_id)
        assert tag == "button", el_id
        assert f'aria-label="{aria}"' in attrs, el_id
        assert re.search(r'<span id="%sText">%s</span>' % (el_id, text), inner), \
            f"#{el_id} must carry a text label, not an icon alone"


def test_app_drives_the_labelled_controls_from_the_model():
    js = (UI / "app.js").read_text(encoding="utf-8")
    for needle in ('$("ctlPauseText").textContent = vm.controls.pause.text',
                   '$("ctlMainText").textContent = vm.controls.main.text',
                   '"aria-label", vm.controls.pause.aria', '"aria-label", vm.controls.main.aria',
                   '"aria-pressed", String(vm.controls.pause.pressed)',
                   '$("ctlPause").onclick', '$("ctlMain").onclick'):
        assert needle in js, needle


TONE_TOKENS = ("--speak", "--listen", "--neutral", "--muted", "--interrupt")


def _tokens(block):
    out = {}
    for name, h in re.findall(r"(--[\w-]+):\s*#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b", block):
        out[name] = "#" + (h if len(h) == 6 else "".join(c * 2 for c in h))
    return out


def _contrast(a, b):
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_indicator_colours_are_tokens_with_contrast_in_light_and_dark():
    css = (UI / "pill.css").read_text(encoding="utf-8")
    light = _tokens(re.search(r"^:root\{([^}]*)\}", css, re.M).group(1))
    dark = _tokens(re.search(r"prefers-color-scheme:\s*dark\)\s*\{\s*:root\{([^}]*)\}", css).group(1))
    for scheme, toks in (("light", light), ("dark", dark)):
        for t in TONE_TOKENS:
            assert t in toks, f"{t} missing in {scheme}"
            # WCAG 1.4.11: a state shown by colour needs 3:1 against its background
            assert _contrast(toks[t], toks["--bg"]) >= 3.0, (scheme, t, toks[t])
        # Interrupted is also written in this colour: text needs 4.5:1
        assert _contrast(toks["--interrupt"], toks["--bg"]) >= 4.5, scheme
        assert len({toks[t] for t in TONE_TOKENS}) == len(TONE_TOKENS), f"{scheme}: two tones share a colour"
    for tone, token in (("speaking", "--speak"), ("listening", "--listen"), ("neutral", "--neutral"),
                        ("muted", "--muted"), ("interrupted", "--interrupt")):
        assert re.search(r"\[data-tone=%s\] \.orb\{[^}]*var\(%s\)" % (tone, token), css), tone


def test_a_labelled_preview_button_sits_beside_the_voice_list():
    html = (UI / "index.html").read_text(encoding="utf-8")
    tag, attrs, inner = _tag(html, "preview")
    assert tag == "button"
    assert 'aria-label="Preview this voice"' in attrs and "title=" in attrs
    assert "Preview" in inner
    row = re.search(r'<div class="row">\s*<select id="voice".*?</div>', html, re.S).group(0)
    assert 'id="preview"' in row, "Preview must be in the voice row"
    tag, attrs, _ = _tag(html, "previewStatus")
    assert 'role="status"' in attrs and 'aria-live="polite"' in attrs


def test_the_indicator_dot_is_labelled_and_driven_by_the_model():
    html = (UI / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<div id="orb" class="orb"([^>]*)>', html)
    assert m and 'role="img"' in m.group(1) and "aria-label=" in m.group(1)
    js = (UI / "app.js").read_text(encoding="utf-8")
    for needle in ("document.body.dataset.tone = vm.indicator.tone",
                   '$("orb").setAttribute("aria-label", ',
                   "M.interruptFlash(", "{ interruptedUntil: interruptedUntil }"):
        assert needle in js, needle
