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
