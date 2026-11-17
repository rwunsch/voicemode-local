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
