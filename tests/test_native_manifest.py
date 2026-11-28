"""The native Windows patch list: patches/manifest.windows.json, `apply.py --native-windows`.

Native Windows has no ffmpeg, so it adds FELIX's three patches (no_ffmpeg_gate,
no_ffmpeg_warning, load_scipy_early) in FELIX's order relative to ours. WSL and Mac
keep ffmpeg and patches/manifest.json, whose output must not change by one byte.

Run with: VM812_SRC=<dir holding pristine 8.12.0 sources, server.py included>
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import io
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"
BEFORE = "d30ec68"  # the last commit before the native list existed
FELIX_THREE = ["patch_no_ffmpeg_gate.py", "patch_no_ffmpeg_warning.py", "patch_load_scipy_early.py"]
# Ours, native only: Windows cannot delete an open temp file (WinError 32). Runs last.
NATIVE_OURS = ["patch_windows_tempfile.py", "patch_native_client_folder.py"]
SOURCES = {
    "control_channel.py": "control_channel.py", "control_socket.py": "control_socket.py",
    "core.py": "core.py", "simple_failover.py": "simple_failover.py", "streaming.py": "streaming.py", "audio_player.py": "audio_player.py",
    "server.py": "server.py", "tools/converse.py": "converse.py", "conch_notify.py": "conch_notify.py",
    "conch_queue.py": "conch_queue.py", "conch.py": "conch.py",
    "config.py": "config.py",
    "tools/configuration_management.py": "configuration_management.py",
}


def _src():
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    src = Path(raw)
    for name in SOURCES.values():
        if not (src / name).is_file():
            pytest.fail(f"VM812_SRC={src} has no {name}")
    return src


def _tree(root, crlf=False):
    pkg = root / "voice_mode"
    (pkg / "tools").mkdir(parents=True)
    for rel, name in SOURCES.items():
        data = (_src() / name).read_bytes()
        (pkg / rel).write_bytes(data.replace(b"\n", b"\r\n") if crlf else data)
    return pkg


def _apply(apply_py, pkg, *extra):
    r = subprocess.run([sys.executable, str(apply_py), "--package-dir", str(pkg), *extra],
                       capture_output=True, text=True, env=dict(os.environ, PYTHONUTF8="1"))
    assert r.returncode == 0, r.stdout + r.stderr
    return r


def _files(pkg):
    return {p.relative_to(pkg).as_posix(): p.read_bytes()
            for p in sorted(pkg.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def _patches_at(commit, dest):
    data = subprocess.run(["git", "-C", str(REPO), "archive", commit, "patches"],
                          capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(dest, filter="data")
    return current_modules(dest / "patches")


def current_modules(old):
    """Lay today's copied modules (manifest.json "copy" steps) over an old patches/ tree.
    These byte tests check the patching mechanism; a module's content changes by design
    (main's 5a6cb8d changed voice_pill_state.py) and is covered by its own tests."""
    steps = json.loads((PATCHES / "manifest.json").read_text(encoding="utf-8"))["steps"]
    for name in {s["copy"] for s in steps if "copy" in s}:
        (old / name).parent.mkdir(parents=True, exist_ok=True)  # "../ui/pill/..." leave patches/
        shutil.copyfile(PATCHES / name, old / name)
    return old


def _steps(name):
    return json.loads((PATCHES / name).read_text(encoding="utf-8"))["steps"]


# -- WSL and Mac: unchanged but for the patches added on purpose ------------

# Steps added to manifest.json since BEFORE, each by design; the rest stays byte-identical.
INTENDED_STEP_LINES = [
    b'    {"patch": "patch_ptt_instant.py",\n'
    b'     "targets": ["control_channel.py", "control_socket.py", "tools/converse.py"],\n'
    b'     "why": "push-to-talk cuts TTS generation, records at once, a stop clears the hold; '
    b'needs hold, barge and pill"},\n',
]
INTENDED_PATCHES = [("patch_ptt_instant.py", ["control_channel.py", "control_socket.py", "tools/converse.py"])]
# 2026-11-07: session_wake.py, copied in after voice_pill_state.py.
INTENDED_STEP_LINES.append(
    b'    {"copy": "session_wake.py", "to": "session_wake.py", "why": "wake an idle Claude Code '
    b'session: push-to-talk hold and the pill\'s Play"},\n')
INTENDED_COPIES = ["session_wake.py"]
# 2026-11-10: the voice server starts the pill window itself (pill_autostart).
INTENDED_STEP_LINES.append(b'    {"copy": "pill_autostart.py", "to": "pill_autostart.py", "why": "the voice server starts the pill window itself"},\n')
INTENDED_STEP_LINES.append(b'    {"copy": "../pill-windows.ps1", "to": "pill-windows.ps1", "why": "the Windows pill window, for pill_autostart"},\n')
INTENDED_STEP_LINES.append(b'    {"copy": "voice_pill_mac.py", "to": "voice_pill_mac.py", "why": "the macOS pill window, for pill_autostart"},\n')
INTENDED_STEP_LINES.append(b'    {"copy": "../ui/pill/index.html", "to": "ui/pill/index.html"},\n')
INTENDED_STEP_LINES.append(b'    {"copy": "../ui/pill/app.js", "to": "ui/pill/app.js"},\n')
INTENDED_STEP_LINES.append(b'    {"copy": "../ui/pill/model.js", "to": "ui/pill/model.js"},\n')
INTENDED_STEP_LINES.append(b'    {"copy": "../ui/pill/pill.css", "to": "ui/pill/pill.css"},\n')
INTENDED_COPIES += ["pill_autostart.py", "pill-windows.ps1", "voice_pill_mac.py",
                    "ui/pill/index.html", "ui/pill/app.js", "ui/pill/model.js", "ui/pill/pill.css"]
# 2026-11-11: per-harness conch wakers (callback is served as wait where none can reach).
INTENDED_STEP_LINES.append(b'    {"copy": "conch_wakers.py", "to": "conch_wakers.py", "why": "per-harness wakers for the conch queue\'s callback mode"},\n')
INTENDED_STEP_LINES.append(b'    {"patch": "patch_conch_wakers.py", "targets": ["tools/converse.py", "conch_notify.py", "conch_queue.py", "conch.py"], "why": "callback is served as wait where no waker can reach the session; wakers before upstream\'s tmux push"},\n')
INTENDED_PATCHES.append(("patch_conch_wakers.py", ["tools/converse.py", "conch_notify.py", "conch_queue.py", "conch.py"]))
INTENDED_PATCHES.append(("patch_session_name.py", ["tools/converse.py"]))   # 2026-11-11: Copilot chat title step
INTENDED_COPIES += ["conch_wakers.py"]
# 2026-11-11: fair holds, MCP progress heartbeat, a cancelled wait is logged.
INTENDED_STEP_LINES.append(b'    {"copy": "conch_hold.py", "to": "conch_hold.py", "why": "fair holds: a hold does not starve a waiting session"},\n')
INTENDED_STEP_LINES.append(b'    {"copy": "mcp_progress.py", "to": "mcp_progress.py", "why": "MCP progress heartbeat so a 180 s client timeout does not cancel converse"},\n')
INTENDED_STEP_LINES.append(b'    {"patch": "patch_conch_fairness.py", "targets": ["tools/converse.py"], "why": "fair holds, progress heartbeat, a cancelled wait is logged; needs conch_hold.py and mcp_progress.py"},\n')
INTENDED_PATCHES.append(("patch_conch_fairness.py", ["tools/converse.py"]))
# 2026-11-12 (Talos C1): only voice settings travel from voicemode.env into the environment.
INTENDED_STEP_LINES.append(b'    {"patch": "patch_env_allowlist.py", "targets": ["config.py", "tools/configuration_management.py"], "why": "only voice settings travel from voicemode.env into the environment; update_config refuses other keys"},\n')
INTENDED_STEP_LINES.append(b'    {"patch": "patch_git_timeout.py", "targets": ["config.py"], "why": "the transcript-saving git call is bounded (5 s, stdin closed): it froze the voice server for 176 s on Windows"},\n')   # 2026-11-13: bounded git call
INTENDED_STEP_LINES.append(b'    {"patch": "patch_env_provider_keys.py", "targets": ["config.py", "tools/configuration_management.py"], "why": "optional: the two provider keys upstream reads may also come from voicemode.env; not in the Windows manifest or FELIX"}\n')
INTENDED_PATCHES.append(("patch_env_allowlist.py", ["config.py", "tools/configuration_management.py"]))
INTENDED_PATCHES.append(("patch_env_provider_keys.py", ["config.py", "tools/configuration_management.py"]))
INTENDED_PATCHES.append(("patch_git_timeout.py", ["config.py"]))   # 2026-11-13: bounded git call
INTENDED_PATCHES.append(("patch_simple_failover.py", ["simple_failover.py"]))   # 2026-11-13: TTS timeout knob (edit 3)
INTENDED_COPIES += ["conch_hold.py", "mcp_progress.py"]
# 2026-11-14: long replies in parts, and a timed-out local TTS request is not sent twice.
INTENDED_STEP_LINES.append(b'    {"copy": "tts_chunks.py", "to": "tts_chunks.py", "why": "a long reply is spoken in parts, one TTS request each (VOICEMODE_TTS_CHUNK_CHARS, default 300)"},\n')
INTENDED_STEP_LINES.append(b'    {"patch": "patch_tts_chunks.py", "targets": ["simple_failover.py"], "why": "simple_tts_failover speaks a long reply in parts; needs tts_chunks.py"},\n')
INTENDED_STEP_LINES.append(b'    {"patch": "patch_tts_no_double_synthesis.py", "targets": ["core.py", "streaming.py"], "why": "a timed-out local TTS request is not sent again buffered, so the server never generates the same speech twice"},\n')
INTENDED_PATCHES.append(("patch_tts_chunks.py", ["simple_failover.py"]))
INTENDED_PATCHES.append(("patch_tts_no_double_synthesis.py", ["core.py", "streaming.py"]))
INTENDED_COPIES += ["tts_chunks.py"]
# 2026-11-17: an uninstalled voice is spoken with an installed one of its language, never a 500.
INTENDED_STEP_LINES.append(b'    {"copy": "voice_languages.py", "to": "voice_languages.py", "why": "which language a local voice speaks, and ISO codes of language names"},\n')
INTENDED_STEP_LINES.append(b'    {"copy": "voice_fallback.py", "to": "voice_fallback.py", "why": "an uninstalled voice is spoken with an installed voice of its language"},\n')
INTENDED_STEP_LINES.append(b'    {"patch": "patch_voice_fallback.py", "targets": ["simple_failover.py", "tools/converse.py"], "why": "an uninstalled voice is spoken with an installed one of its language, never a 500, and converse says so; needs voice_fallback.py"},\n')
INTENDED_PATCHES.append(("patch_voice_fallback.py", ["simple_failover.py", "tools/converse.py"]))
INTENDED_COPIES += ["voice_languages.py", "voice_fallback.py"]
# 2026-11-17: echo labels from settings; STT language follows the pill.
INTENDED_STEP_LINES.append(b'    {"patch": "patch_echo_labels.py", "targets": ["tools/converse.py"], "why": "the transcript echo lines\' labels come from VOICEMODE_ECHO_ASSISTANT_LABEL and VOICEMODE_ECHO_USER_LABEL"},\n')
INTENDED_STEP_LINES.append(b'    {"patch": "patch_stt_language.py", "targets": ["simple_failover.py"], "why": "the STT request passes the pill\'s language for the session; needs voice_pill_state.py and voice_languages.py"},\n')
INTENDED_PATCHES.append(("patch_echo_labels.py", ["tools/converse.py"]))
INTENDED_PATCHES.append(("patch_stt_language.py", ["simple_failover.py"]))
# 2026-11-17: scipy.signal is imported at start on WSL/Mac too (a cold import outlasts the stall backstop).
INTENDED_STEP_LINES.append(b'    {"patch": "patch_load_scipy_early.py", "targets": ["tools/converse.py"],\n'
                           b'     "why": "import scipy.signal at start, not mid-recording: a cold import outlasts the 5 s stall backstop (voicemode#545)"},\n')
INTENDED_PATCHES.append(("patch_load_scipy_early.py", ["tools/converse.py"]))
# 2026-11-18: the voice_sensitivity override, resolved at each recording.
INTENDED_STEP_LINES.append(b'    {"copy": "voice_sensitivity.py", "to": "voice_sensitivity.py", "why": "the voice_sensitivity tool\'s override of VAD aggressiveness and silence length (<base dir>/sensitivity.json)"},\n')
INTENDED_STEP_LINES.append(b'    {"patch": "patch_vad_override.py", "targets": ["tools/converse.py"], "why": "each recording resolves VAD aggressiveness and silence length: converse argument, else the override, else config; needs voice_sensitivity.py"},\n')
INTENDED_PATCHES.append(("patch_vad_override.py", ["tools/converse.py"]))
INTENDED_COPIES += ["voice_sensitivity.py"]
# 2026-11-18: a mic stream that fails to start is retried once; the fallback ends on cancel.
INTENDED_STEP_LINES.append(b'    {"patch": "patch_audio_recover.py", "targets": ["tools/converse.py"], "why": "a mic stream that fails to start (Wait timed out, -9987) is re-initialised and retried once with silence detection; the fixed-length fallback ends on cancel and VOICEMODE_FALLBACK_RECORD_MAX_S caps it"},\n')
INTENDED_PATCHES.append(("patch_audio_recover.py", ["tools/converse.py"]))
# 2026-11-27: measured orb levels and a playback-scoped interrupt.
ORB_ASSETS = ["orb-pill.css", "orb-pill.js", "orb-felix.png", "orb-felix-motion.webp",
              "spectrum.js", "spectrum.NOTICES.txt"]
for asset in ORB_ASSETS:
    path = f"ui/pill/{asset}"
    INTENDED_STEP_LINES.append(f'    {{"copy": "../{path}", "to": "{path}"}},\n'.encode())
    INTENDED_COPIES.append(path)
INTENDED_STEP_LINES.append(b'    {"patch": "patch_voice_orb.py", "targets": ["control_channel.py", "control_socket.py", "tools/converse.py", "streaming.py", "audio_player.py", "core.py"],\n'
                           b'     "why": "measure speaker levels and bind Pill interrupt clicks to one playback"},\n')
INTENDED_PATCHES.append(("patch_voice_orb.py", ["control_channel.py", "control_socket.py", "tools/converse.py",
                                              "streaming.py", "audio_player.py", "core.py"]))
# 2026-11-27 (FELIX #2095): audio-device calls get a deadline; a stuck voice server exits.
INTENDED_STEP_LINES.append(b'    {"copy": "audio_deadline.py", "to": "audio_deadline.py"},\n')
INTENDED_STEP_LINES.append(
    b'    {"patch": "patch_audio_deadline.py", "targets": ["core.py", "tools/converse.py", "control_socket.py"],\n'
    b'     "why": "audio-device calls off the event loop with a deadline; loop watchdog exits a stuck server (FELIX #2095)"},\n')
INTENDED_PATCHES.append(("patch_audio_deadline.py", ["core.py", "tools/converse.py", "control_socket.py"]))
INTENDED_COPIES += ["audio_deadline.py"]
# Text edits those additions force on lines that were already there: (now, before).
INTENDED_EDITS = [
    (b'    "audio_player.py",\n', b''),
    (b'    "streaming.py",\n    "conch_notify.py",\n    "conch_queue.py",\n    "conch.py",\n    "config.py",\n    "tools/configuration_management.py"\n', b'    "streaming.py"\n'),
    (b'"why": "MCP tool for switching voice barge-in"},\n', b'"why": "MCP tool for switching voice barge-in"}\n'),
]


def test_default_manifest_file_is_unchanged():
    old = subprocess.run(["git", "-C", str(REPO), "show", f"{BEFORE}:patches/manifest.json"],
                         capture_output=True, check=True).stdout
    now = (PATCHES / "manifest.json").read_bytes()
    for lines in INTENDED_STEP_LINES:
        assert now.count(lines) == 1
        now = now.replace(lines, b"")
    for after, before in INTENDED_EDITS:
        assert now.count(after) == 1
        now = now.replace(after, before)
    assert now == old


def test_default_output_is_byte_identical_to_before(tmp_path):
    old = _patches_at(BEFORE, tmp_path / "old")
    before, now = _tree(tmp_path / "a"), _tree(tmp_path / "b")
    _apply(old / "apply.py", before)
    for script, targets in INTENDED_PATCHES:
        for rel in targets:
            r = subprocess.run([sys.executable, str(PATCHES / script), str(before / rel)],
                               capture_output=True, text=True, env=dict(os.environ, PYTHONUTF8="1"))
            assert r.returncode == 0, r.stdout + r.stderr
    sources = {s["to"]: s["copy"] for s in _steps("manifest.json") if "copy" in s}
    for name in INTENDED_COPIES:
        (before / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(PATCHES / sources[name], before / name)
    _apply(PATCHES / "apply.py", now)
    assert _files(now) == _files(before)
    assert "FELIX: NO FFMPEG" not in (now / "tools/converse.py").read_text(encoding="utf-8")


def test_default_manifest_loads_scipy_before_recording(tmp_path):
    """2026-11-17: the recording loop imports scipy.signal on its first chunk, with the 5 s
    stall backstop already running; a cold import took 6.5 s and ended the turn after one
    chunk. WSL/Linux/Mac need the same early import native Windows already had."""
    names = [s.get("patch") or s.get("copy") for s in _steps("manifest.json")]
    assert names.count("patch_load_scipy_early.py") == 1
    assert names.index("patch_simple_failover.py") < names.index("patch_load_scipy_early.py") \
        < names.index("patch_session_name.py")
    pkg = _tree(tmp_path)
    _apply(PATCHES / "apply.py", pkg)
    once = _files(pkg)
    assert b"FELIX: LOAD SCIPY EARLY" in once["tools/converse.py"]
    assert b"FELIX: NO FFMPEG" not in once["tools/converse.py"]   # ffmpeg stays on WSL/Mac
    _apply(PATCHES / "apply.py", pkg)
    assert _files(pkg) == once   # idempotent


# -- native Windows ---------------------------------------------------------

def test_native_list_is_the_default_plus_felix_three_in_felixs_place():
    default, native = _steps("manifest.json"), _steps("manifest.windows.json")
    added = [s for s in native if s.get("patch") in FELIX_THREE]
    assert [s["patch"] for s in added] == FELIX_THREE
    ours = [s for s in native if s.get("patch") in NATIVE_OURS]
    assert native[-len(ours):] == ours and [s["patch"] for s in ours] == NATIVE_OURS
    assert ours[0]["targets"] == ["core.py", "streaming.py"]
    assert ours[1]["targets"] == ["tools/converse.py"]
    # The provider-keys step is deliberately default-only (FELIX never sets a provider key).
    # Likewise load_scipy_early: the default list now has it too (2026-11-17), right after
    # simple_failover; native keeps FELIX's own position for it (third of its three), and the
    # patch is idempotent, so the two lists agree on the result.
    assert [s for s in native if s not in added and s not in ours] == \
        [s for s in default if s.get("patch") not in ("patch_env_provider_keys.py", "patch_load_scipy_early.py")]
    # FELIX's order: listen_overrun, turns_playback_stop, simple_failover, then its three.
    names = [s.get("patch") or s.get("copy") for s in native]
    at = names.index("patch_simple_failover.py")
    assert names[at + 1:at + 4] == FELIX_THREE
    targets = {s["patch"]: s["targets"] for s in added}
    assert targets == {"patch_no_ffmpeg_gate.py": ["tools/converse.py"],
                       "patch_no_ffmpeg_warning.py": ["server.py"],
                       "patch_load_scipy_early.py": ["tools/converse.py"]}
    unshare = json.loads((PATCHES / "manifest.windows.json").read_text(encoding="utf-8"))["unshare"]
    assert "server.py" in unshare


@pytest.mark.parametrize("crlf", [False, True], ids=["LF", "CRLF"])
def test_native_list_applies_cleanly_and_is_idempotent(tmp_path, crlf):
    pkg = _tree(tmp_path, crlf=crlf)
    _apply(PATCHES / "apply.py", pkg, "--native-windows")
    once = _files(pkg)
    _apply(PATCHES / "apply.py", pkg, "--native-windows")
    assert _files(pkg) == once
    converse = once["tools/converse.py"].decode("utf-8")
    assert "FELIX: NO FFMPEG" in converse and "FELIX: LOAD SCIPY EARLY" in converse
    assert "FELIX: NO FFMPEG WARNING" in once["server.py"].decode("utf-8")
    assert "VML: WINDOWS TEMPFILE" in once["core.py"].decode("utf-8")
    assert "VML: WINDOWS TEMPFILE" in once["streaming.py"].decode("utf-8")
    for rel in SOURCES:
        data = once[rel]
        if crlf:
            assert b"\n" not in data.replace(b"\r\n", b""), f"{rel} lost its CRLF endings"
        else:
            assert b"\r\n" not in data, rel


def test_native_output_differs_from_default_only_by_felixs_three_and_the_tempfile_fix(tmp_path):
    default, native = _tree(tmp_path / "d"), _tree(tmp_path / "n")
    _apply(PATCHES / "apply.py", default)
    _apply(PATCHES / "apply.py", native, "--native-windows")
    d, n = _files(default), _files(native)
    assert set(d) == set(n)
    assert {rel for rel in d if d[rel] != n[rel]} == {"tools/converse.py", "server.py", "core.py", "streaming.py",
                                                     "config.py", "tools/configuration_management.py"}
    assert "VML: ENV PROVIDER KEYS" in d["config.py"].decode("utf-8")
    assert "VML: ENV PROVIDER KEYS" not in n["config.py"].decode("utf-8")
    assert "VML: WINDOWS TEMPFILE" not in d["core.py"].decode("utf-8")
    assert "os.unlink(tmp_file.name)" not in n["core.py"].decode("utf-8")


@pytest.mark.parametrize("script,rel", [("patch_no_ffmpeg_gate.py", "tools/converse.py"),
                                        ("patch_no_ffmpeg_warning.py", "server.py"),
                                        ("patch_load_scipy_early.py", "tools/converse.py")])
def test_our_copies_keep_crlf(tmp_path, script, rel):
    """The case test_patch_trees marks xfail for FELIX's own scripts, which write LF."""
    pkg = _tree(tmp_path, crlf=True)
    r = subprocess.run([sys.executable, str(PATCHES / script), str(pkg / rel)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    data = (pkg / rel).read_bytes()
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")


def test_our_copies_record_provenance():
    for name in FELIX_THREE:
        text = (PATCHES / name).read_text(encoding="utf-8")
        assert "felix-framework" in text and "aea841ffd" in text, name


# -- the ffmpeg check in converse, with no ffmpeg on PATH --------------------

GATE_START = "ffmpeg_available = "
GATE_END = 'return f"❌ Error: {error_msg}"'


def _gate(converse_text):
    """converse's ffmpeg check, lifted out of the function into one we can call."""
    lines = converse_text.replace("\r\n", "\n").split("\n")
    start = next(i for i, l in enumerate(lines) if l.strip().startswith(GATE_START))
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == GATE_END)
    body = textwrap.dedent("\n".join(lines[start:end + 1]))
    body = body.replace("from ..utils.ffmpeg_check import get_install_instructions",
                        "get_install_instructions = lambda: 'install ffmpeg'")
    return "def gate():\n" + textwrap.indent(body, "    ") + "\n    return 'passed'\n"


def _run_gate(converse_text, no_ffmpeg_path, monkeypatch):
    """With PATH holding no ffmpeg: what server.py would set, then converse's check."""
    monkeypatch.setenv("PATH", str(no_ffmpeg_path))
    available = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
    config = type("config", (), {"FFMPEG_AVAILABLE": available})
    namespace = {"voice_mode": type("voice_mode", (), {"config": config}),
                 "logger": type("logger", (), {"error": staticmethod(lambda *a: None)})}
    exec(_gate(converse_text), namespace)
    return available, namespace["gate"]()


def test_ffmpeg_check_passes_without_ffmpeg_on_path_natively(tmp_path, monkeypatch):
    empty = tmp_path / "bin"
    empty.mkdir()
    pristine = (_src() / "converse.py").read_text(encoding="utf-8")
    available, result = _run_gate(pristine, empty, monkeypatch)
    assert available is False                     # the simulated PATH really has no ffmpeg
    assert result.startswith("❌ Error: FFmpeg is required")  # and pristine refuses

    pkg = _tree(tmp_path / "native")
    _apply(PATCHES / "apply.py", pkg, "--native-windows")
    native = (pkg / "tools/converse.py").read_text(encoding="utf-8")
    available, result = _run_gate(native, empty, monkeypatch)
    assert available is False and result == "passed"


@pytest.mark.parametrize("name", FELIX_THREE)
def test_our_copies_fail_loudly_on_drift_and_write_nothing(tmp_path, name):
    target = tmp_path / "target.py"
    target.write_bytes(b"x = 1\r\n")
    r = subprocess.run([sys.executable, str(PATCHES / name), str(target)], capture_output=True, text=True)
    assert r.returncode != 0 and r.stderr.strip()
    assert target.read_bytes() == b"x = 1\r\n"
