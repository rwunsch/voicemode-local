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
