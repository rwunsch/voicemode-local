"""The patch set on every tree it has to work on, as `python <script> <target>`.

Four trees (design spec section 7 and section 10 point 9):

  pristine        voice-mode 8.12.0, then barge, pill and transport
  felix           pristine plus FELIX's patches, in FELIX's order
  felix_ship      FELIX's patches, then barge, pill and transport (what FELIX ships)
  full            voicemode-local's whole manifest, through patches/apply.py

Each is applied twice: the second pass must change nothing. Then every file must
compile, carry its markers, and name no global the pristine file does not define
(a patch that compiles can still call a helper only another patch supplies).

Run with: VM812_SRC=<dir holding pristine 8.12.0 sources, server.py included> pytest tests/test_patch_trees.py

FELIX's scripts are copied verbatim into tests/fixtures/felix_patches/ from
felix-framework aea841ffd (feat/voice-opt-in, plugins/voicemode/patches/); its
order is that commit's dependencies.json `patches` list.
"""
import builtins
import json
import os
import shutil
import subprocess
import symtable
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"
FELIX = Path(__file__).resolve().parent / "fixtures" / "felix_patches"

# relative path in the package -> file name in VM812_SRC
SOURCES = {
    "control_channel.py": "control_channel.py",
    "control_socket.py": "control_socket.py",
    "core.py": "core.py",
    "simple_failover.py": "simple_failover.py",
    "streaming.py": "streaming.py",
    "server.py": "server.py",
    "tools/converse.py": "converse.py",
    "conch_notify.py": "conch_notify.py",
    "conch_queue.py": "conch_queue.py",
    "conch.py": "conch.py",
}

# felix-framework aea841ffd, plugins/voicemode/dependencies.json "patches", in order.
FELIX_ORDER = [
    (PATCHES / "patch_listen_overrun.py", "tools/converse.py"),
    (PATCHES / "patch_turns_playback_stop.py", "tools/converse.py"),
    (PATCHES / "patch_simple_failover.py", "simple_failover.py"),
    (FELIX / "patch_no_ffmpeg_gate.py", "tools/converse.py"),
    (FELIX / "patch_no_ffmpeg_warning.py", "server.py"),
    (FELIX / "patch_load_scipy_early.py", "tools/converse.py"),
]

OURS = (
    [(PATCHES / "patch_voice_barge.py", t)
     for t in ("control_channel.py", "control_socket.py", "core.py", "tools/converse.py")]
    + [(PATCHES / "patch_voice_pill.py", t)
       for t in ("control_channel.py", "control_socket.py", "tools/converse.py", "streaming.py")]
    + [(PATCHES / "patch_control_transport.py", "control_socket.py")]
)

BARGE = "voicemode-local voice barge"
PILL = "voicemode-local voice pill"
TRANSPORT = "voicemode-local control transport"
OURS_MARKERS = {
    "control_channel.py": [BARGE, PILL],
    "control_socket.py": [BARGE, PILL, TRANSPORT],
    "core.py": [BARGE],
    "tools/converse.py": [BARGE, PILL],
    "streaming.py": [PILL],
}
FELIX_MARKERS = {
    "tools/converse.py": ["voicemode-local listen overrun", "voicemode-local turns playback stop",
                          "FELIX: NO FFMPEG", "FELIX: LOAD SCIPY EARLY"],
    "simple_failover.py": ["NO SILENT SWAP (voicemode-local)"],
    "server.py": ["FELIX: NO FFMPEG WARNING"],
}
HOLD = "voicemode-local ptt hold"
INSTANT = "voicemode-local ptt instant"


def _src():
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    src = Path(raw)
    for name in SOURCES.values():
        if not (src / name).is_file():
            pytest.fail(f"VM812_SRC={src} has no {name}")
    return src


def make_tree(root: Path, crlf: bool = False) -> Path:
    pkg = root / "voice_mode"
    (pkg / "tools").mkdir(parents=True)
    src = _src()
    for rel, name in SOURCES.items():
        data = (src / name).read_bytes()
        if crlf:
            data = data.replace(b"\n", b"\r\n")
        (pkg / rel).write_bytes(data)
    return pkg


def run_patch(script: Path, target: Path, **kw):
    env = dict(os.environ, PYTHONUTF8="1")
    return subprocess.run([sys.executable, str(script), str(target)],
                          capture_output=True, text=True, env=env, **kw)


def apply_seq(pkg: Path, seq):
    for script, rel in seq:
        r = run_patch(script, pkg / rel)
        assert r.returncode == 0, f"{script.name} on {rel}: {r.stdout}{r.stderr}"


def run_apply_py(pkg: Path):
    r = subprocess.run([sys.executable, str(PATCHES / "apply.py"), "--package-dir", str(pkg)],
                       capture_output=True, text=True, env=dict(os.environ, PYTHONUTF8="1"))
    assert r.returncode == 0, r.stdout + r.stderr
    return r


def snapshot(pkg: Path) -> dict:
    return {p.relative_to(pkg).as_posix(): p.read_bytes()
            for p in sorted(pkg.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def undefined_globals(source: str) -> set:
    """Names a module reads as globals that nothing in it (or builtins) defines."""
    top = symtable.symtable(source, "<patched>", "exec")
    defined = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__",
                                    "__loader__", "__package__", "__builtins__", "__path__"}
    for s in top.get_symbols():
        if s.is_assigned() or s.is_imported() or s.is_namespace():
            defined.add(s.get_name())
    missing = set()

    def walk(table):
        for s in table.get_symbols():
            if not s.is_referenced() or s.get_name() in defined:
                continue
            if table is top or s.is_global():
                missing.add(s.get_name())
        for child in table.get_children():
            walk(child)
    walk(top)
    return missing


def _apply_tree(kind: str, root: Path, crlf: bool = False) -> Path:
    pkg = make_tree(root, crlf=crlf)
    if kind == "full":
        run_apply_py(pkg)
    else:
        apply_seq(pkg, {"pristine": OURS, "felix": FELIX_ORDER,
                        "felix_ship": FELIX_ORDER + OURS}[kind])
    return pkg


def _reapply(kind: str, pkg: Path):
    if kind == "full":
        run_apply_py(pkg)
    else:
        apply_seq(pkg, {"pristine": OURS, "felix": FELIX_ORDER,
                        "felix_ship": FELIX_ORDER + OURS}[kind])


EXPECTED_MARKERS = {
    "pristine": OURS_MARKERS,
    "felix": FELIX_MARKERS,
    "felix_ship": {k: OURS_MARKERS.get(k, []) + FELIX_MARKERS.get(k, [])
                   for k in set(OURS_MARKERS) | set(FELIX_MARKERS)},
    "full": {
        "control_channel.py": [HOLD, BARGE, PILL, INSTANT],
        "control_socket.py": [BARGE, PILL, TRANSPORT, INSTANT],
        "core.py": ["voicemode-local hold barge-in", BARGE],
        "streaming.py": ["voicemode-local hold barge-in", PILL],
        "simple_failover.py": ["NO SILENT SWAP (voicemode-local)"],
        "tools/converse.py": ["voicemode-local listen overrun", "voicemode-local turns playback stop",
                              "voicemode-local session name", HOLD, "LANGUAGE-LOCKED VOICES",
                              BARGE, PILL, INSTANT],
    },
}

TREES = ["pristine", "felix", "felix_ship", "full"]


@pytest.fixture(scope="module")
def trees(tmp_path_factory):
    out = {}
    for kind in TREES:
        pkg = _apply_tree(kind, tmp_path_factory.mktemp(kind))
        first = snapshot(pkg)
        _reapply(kind, pkg)
        out[kind] = (pkg, first, snapshot(pkg))
    return out


@pytest.mark.parametrize("kind", TREES)
def test_second_pass_changes_nothing(trees, kind):
    _, first, second = trees[kind]
    assert first == second


@pytest.mark.parametrize("kind", TREES)
def test_every_file_compiles(trees, kind):
    pkg, _, files = trees[kind]
    for rel, data in files.items():
        if rel.endswith(".py"):
            compile(data.decode("utf-8"), str(pkg / rel), "exec")


@pytest.mark.parametrize("kind", TREES)
def test_markers(trees, kind):
    _, _, files = trees[kind]
    for rel, markers in EXPECTED_MARKERS[kind].items():
        text = files[rel].decode("utf-8")
        for m in markers:
            assert m in text, f"{kind}: {rel} lacks {m!r}"
    if kind != "full":  # the no-hold trees must not have grown hold code from somewhere
        assert all(HOLD not in d.decode("utf-8") for d in files.values() if d)


@pytest.mark.parametrize("kind", TREES)
def test_no_name_only_another_patch_defines(trees, kind):
    _, _, files = trees[kind]
    src = _src()
    for rel, name in SOURCES.items():
        pristine = undefined_globals((src / name).read_text(encoding="utf-8"))
        patched = undefined_globals(files[rel].decode("utf-8"))
        assert patched <= pristine, f"{kind}: {rel} reads undefined {sorted(patched - pristine)}"


def test_hold_tree_output_of_ours_is_unchanged_by_the_no_hold_paths(trees):
    """With the hold patches present, barge's channel edits are today's hold-anchored ones."""
    _, _, files = trees["full"]
    channel = files["control_channel.py"].decode("utf-8")
    assert ("COMMAND_HOLD_END = \"hold_end\"\n"
            f"# {BARGE}: skip_forward, but a no-op unless TTS is playing.\n"
            "COMMAND_BARGE_IN = \"barge_in\"\n") in channel
    converse = files["tools/converse.py"].decode("utf-8")
    assert "_voice_pill_session_name" not in converse  # the session-name patch supplies the label


def test_no_hold_channel_dispatches_barge_in(trees):
    _, _, files = trees["felix_ship"]
    ns = {}
    exec(compile(files["control_channel.py"].decode("utf-8"), "control_channel.py", "exec"), ns)
    st = ns["ControlState"]()
    line = '{"command": "barge_in"}'
    assert ns["parse_command"](line).command == "barge_in"
    ns["parse_command"](line).apply_to(st)
    assert st.state != "skip_forward"  # nothing playing: a no-op
    st.set_speaking(True)
    ns["parse_command"](line).apply_to(st)
    assert st.state == "skip_forward" and st.take_barged() is True


def test_no_session_name_patch_label(trees, monkeypatch):
    """Without patch_session_name the pill labels the session by env, else the folder."""
    _, _, files = trees["felix_ship"]
    src = files["tools/converse.py"].decode("utf-8")
    start = src.index("def _voice_pill_session_name() -> str:")
    end = src.index("\n\n\n", start)
    ns = {"os": os, "Path": Path}
    exec(src[start:end], ns)
    monkeypatch.setenv("VOICEMODE_SESSION_NAME", "  felix-chat  ")
    assert ns["_voice_pill_session_name"]() == "felix-chat"
    monkeypatch.delenv("VOICEMODE_SESSION_NAME")
    monkeypatch.chdir(REPO)
    assert ns["_voice_pill_session_name"]() == REPO.name


# --- line endings ------------------------------------------------------------

@pytest.mark.parametrize("kind", ["pristine", "full"])
def test_crlf_input_gives_the_lf_output_in_crlf(trees, tmp_path, kind):
    pkg = _apply_tree(kind, tmp_path, crlf=True)
    crlf_files = snapshot(pkg)
    _reapply(kind, pkg)
    assert snapshot(pkg) == crlf_files
    _, _, lf_files = trees[kind]
    for rel in SOURCES:
        assert crlf_files[rel] == lf_files[rel].replace(b"\n", b"\r\n"), rel


def test_felix_patches_tolerate_crlf(tmp_path):
    pkg = _apply_tree("felix_ship", tmp_path, crlf=True)
    for rel, markers in EXPECTED_MARKERS["felix_ship"].items():
        text = (pkg / rel).read_bytes().decode("utf-8")
        assert all(m in text for m in markers), rel


@pytest.mark.xfail(strict=True, reason="FELIX's scripts write LF whatever they read "
                   "(aea841ffd); flips when FELIX keeps the file's endings")
@pytest.mark.parametrize("script,rel", [(s, r) for s, r in FELIX_ORDER if s.parent == FELIX])
def test_felix_patches_keep_crlf(tmp_path, script, rel):
    pkg = make_tree(tmp_path, crlf=True)
    assert run_patch(script, pkg / rel).returncode == 0
    assert b"\r\n" in (pkg / rel).read_bytes() and b"\n" not in (pkg / rel).read_bytes().replace(b"\r\n", b"")


# --- drift -------------------------------------------------------------------

def _manifest_patch_pairs():
    manifest = json.loads((PATCHES / "manifest.json").read_text(encoding="utf-8"))
    pairs = [(PATCHES / step["patch"], rel) for step in manifest["steps"] if "patch" in step
             for rel in step["targets"]]
    return pairs + [(s, r) for s, r in FELIX_ORDER if s.parent == FELIX]


@pytest.mark.parametrize("script,rel", _manifest_patch_pairs(),
                         ids=lambda v: v.name if isinstance(v, Path) else v)
def test_drift_fails_loudly_and_writes_nothing(tmp_path, script, rel):
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x = 1\n")
    r = run_patch(script, target)
    assert r.returncode != 0, f"{script.name} accepted a file without its anchors"
    assert r.stderr.strip(), "no message on stderr"
    assert target.read_bytes() == b"x = 1\n"


def test_barge_with_hold_marker_but_no_hold_anchors_is_drift(tmp_path):
    """The hold marker selects the hold anchors; it must not fall back to pristine ones."""
    pkg = make_tree(tmp_path)
    cc = pkg / "control_channel.py"
    cc.write_bytes(cc.read_bytes() + f"# {HOLD}\n".encode())
    before = cc.read_bytes()
    r = run_patch(PATCHES / "patch_voice_barge.py", cc)
    assert r.returncode != 0 and "ANCHOR DRIFT" in r.stderr
    assert cc.read_bytes() == before


def test_pill_without_barge_is_drift(tmp_path):
    pkg = make_tree(tmp_path)
    for rel in ("control_channel.py", "control_socket.py", "tools/converse.py"):
        before = (pkg / rel).read_bytes()
        r = run_patch(PATCHES / "patch_voice_pill.py", pkg / rel)
        assert r.returncode != 0 and "ANCHOR DRIFT" in r.stderr, rel
        assert (pkg / rel).read_bytes() == before
