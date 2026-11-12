"""patches/apply.py and its manifest against the shell apply.sh they replaced.

The release-path check: on a scratch venv of pristine voice-mode 8.12.0, apply.py
(directly and through the delegating apply.sh) must leave every file byte-identical,
with the same mode, to what apply.sh at BASELINE produced. BASELINE is the last
commit before apply.py existed; its patches/ directory is taken from git, so the
comparison is computed, not read from a stored answer.

Run with: VM812_SRC=<dir holding pristine 8.12.0 sources> pytest tests/test_apply_py.py
"""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"
BASELINE = "8055781"  # last apply.sh that did the work itself

sys.path.insert(0, str(PATCHES))
import apply as apply_py  # noqa: E402

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
    "config.py": "config.py",
    "tools/configuration_management.py": "configuration_management.py",
}


def _src():
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    return Path(raw)


def make_venv(root: Path, pyver: str = "python3.12") -> Path:
    """A venv-shaped dir: bin/python is this interpreter, voice_mode is pristine."""
    pkg = root / "venv" / "lib" / pyver / "site-packages" / "voice_mode"
    (pkg / "tools").mkdir(parents=True)
    src = _src()
    for rel, name in SOURCES.items():
        if (src / name).is_file():
            shutil.copyfile(src / name, pkg / rel)
    (root / "venv" / "bin").mkdir()
    (root / "venv" / "bin" / "python").symlink_to(sys.executable)
    return root / "venv"


def pkg_of(venv: Path) -> Path:
    return next((venv / "lib").glob("python*/site-packages/voice_mode"))


def state(pkg: Path) -> dict:
    return {p.relative_to(pkg).as_posix(): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
            for p in sorted(pkg.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def baseline_patches(dest: Path) -> Path:
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout: the baseline apply.sh cannot be read")
    r = subprocess.run(["git", "-C", str(REPO), "archive", BASELINE, "patches"],
                       capture_output=True)
    assert r.returncode == 0, f"baseline {BASELINE} missing: {r.stderr.decode()}"
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
        tf.extractall(dest, filter="data") if hasattr(tarfile, "data_filter") else tf.extractall(dest)
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


# Patches added to manifest.json on purpose since the baseline, each applied to the
# baseline's output so everything else must still match to the byte. Extend this list,
# never loosen the comparison.
INTENDED_SINCE_BASELINE = [
    ("patch_ptt_instant.py", ["control_channel.py", "control_socket.py", "tools/converse.py"]),
    ("patch_conch_wakers.py", ["tools/converse.py", "conch_notify.py", "conch_queue.py", "conch.py"]),   # 2026-11-11
    ("patch_session_name.py", ["tools/converse.py"]),   # 2026-11-11: adds the Copilot chat title step
    ("patch_conch_fairness.py", ["tools/converse.py"]),   # 2026-11-11: fair holds, progress
    ("patch_env_allowlist.py", ["config.py", "tools/configuration_management.py"]),   # 2026-11-12: Talos C1
    ("patch_env_provider_keys.py", ["config.py", "tools/configuration_management.py"]),   # 2026-11-12: optional
]
# Modules added as "copy" steps since the baseline (2026-11-07: session_wake.py).
INTENDED_COPIES_SINCE_BASELINE = ["session_wake.py"]
# 2026-11-10: the voice server starts the pill window itself (pill_autostart).
INTENDED_COPIES_SINCE_BASELINE += ["pill_autostart.py", "pill-windows.ps1", "voice_pill_mac.py",
                                   "ui/pill/index.html", "ui/pill/app.js", "ui/pill/model.js",
                                   "ui/pill/pill.css"]
INTENDED_COPIES_SINCE_BASELINE += ["conch_wakers.py"]   # 2026-11-11
INTENDED_COPIES_SINCE_BASELINE += ["conch_hold.py", "mcp_progress.py"]   # 2026-11-11


def apply_intended(pkg: Path) -> None:
    sources = {s["to"]: s["copy"] for s in json.loads(
        (PATCHES / "manifest.json").read_text(encoding="utf-8"))["steps"] if "copy" in s}
    for name in INTENDED_COPIES_SINCE_BASELINE:
        (pkg / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(PATCHES / sources[name], pkg / name)
    for script, targets in INTENDED_SINCE_BASELINE:
        for rel in targets:
            r = subprocess.run([sys.executable, str(PATCHES / script), str(pkg / rel)],
                               capture_output=True, text=True, env=dict(os.environ, PYTHONUTF8="1"))
            assert r.returncode == 0, r.stdout + r.stderr


def without_intended_log(out: str) -> str:
    copied = tuple(f"[patches] installed {name}" for name in INTENDED_COPIES_SINCE_BASELINE)
    return "".join(line for line in out.splitlines(keepends=True)
                   if "(ptt instant)" not in line and not line.strip().startswith(copied))


def hardlink_core(venv: Path, outside: Path) -> None:
    os.link(pkg_of(venv) / "core.py", outside)


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    root = tmp_path_factory.mktemp("baseline")
    venv = make_venv(root)
    old = baseline_patches(root / "old")
    hardlink_core(venv, root / "core_link.py")
    r = subprocess.run(["bash", str(old / "apply.sh"), str(venv)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    apply_intended(pkg_of(venv))
    return root, venv, r.stdout


def test_apply_py_is_byte_identical_to_the_baseline_apply_sh(baseline, tmp_path):
    _, old_venv, _ = baseline
    venv = make_venv(tmp_path)
    r = subprocess.run([sys.executable, str(PATCHES / "apply.py"), str(venv)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    old, new = state(pkg_of(old_venv)), state(pkg_of(venv))
    assert sorted(old) == sorted(new)
    assert [k for k in old if old[k] != new[k]] == []


def test_delegating_apply_sh_is_byte_identical_to_the_baseline(baseline, tmp_path):
    _, old_venv, old_out = baseline
    venv = make_venv(tmp_path)
    hardlink_core(venv, tmp_path / "core_link.py")
    r = subprocess.run(["bash", str(PATCHES / "apply.sh"), str(venv)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert state(pkg_of(old_venv)) == state(pkg_of(venv))
    # Same log too, once the venv paths are made comparable.
    assert without_intended_log(r.stdout).replace(str(venv), "<venv>") == old_out.replace(str(old_venv), "<venv>")


def test_hardlinks_are_broken_before_patching(baseline, tmp_path):
    root, old_venv, _ = baseline
    pristine_core = (_src() / "core.py").read_bytes()
    assert (root / "core_link.py").read_bytes() == pristine_core  # the baseline kept it private
    venv = make_venv(tmp_path)
    link = tmp_path / "core_link.py"
    hardlink_core(venv, link)
    r = subprocess.run([sys.executable, str(PATCHES / "apply.py"), str(venv)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"[patches] unshared hardlink: {pkg_of(venv) / 'core.py'}" in r.stdout
    assert link.read_bytes() == pristine_core
    assert os.stat(pkg_of(venv) / "core.py").st_nlink == 1
    assert os.stat(link).st_nlink == 1


def test_every_patch_target_is_unshared_first():
    m = json.loads((PATCHES / "manifest.json").read_text(encoding="utf-8"))
    targets = {t for s in m["steps"] if "patch" in s for t in s["targets"]}
    assert targets <= set(m["unshare"])


def test_manifest_names_only_files_that_exist():
    m = json.loads((PATCHES / "manifest.json").read_text(encoding="utf-8"))
    for s in m["steps"]:
        name = s.get("patch") or s["copy"]
        assert (PATCHES / name).is_file(), name


def test_a_missing_listed_file_is_an_error_not_a_skip(tmp_path):
    pkg = tmp_path / "voice_mode"
    pkg.mkdir()
    manifest = {"unshare": [], "steps": [{"copy": "no_such_module.py", "to": "x.py"}]}
    out = io.StringIO()
    assert apply_py.run(pkg, manifest, out=out) != 0
    assert "no_such_module.py" in out.getvalue()


def test_stops_at_the_first_failing_patch(tmp_path):
    pkg = tmp_path / "voice_mode"
    (pkg / "tools").mkdir(parents=True)
    (pkg / "tools" / "converse.py").write_text("x = 1\n")
    manifest = {"unshare": [], "steps": [
        {"patch": "patch_listen_overrun.py", "targets": ["tools/converse.py"]},
        {"copy": "ptt_core.py", "to": "ptt_core.py"},
    ]}
    assert apply_py.run(pkg, manifest, out=io.StringIO()) != 0
    assert not (pkg / "ptt_core.py").exists()


def test_finds_the_package_in_both_venv_layouts(tmp_path):
    posix = tmp_path / "a" / "lib" / "python3.11" / "site-packages" / "voice_mode"
    win = tmp_path / "b" / "Lib" / "site-packages" / "voice_mode"
    posix.mkdir(parents=True)
    win.mkdir(parents=True)
    assert apply_py.find_package(tmp_path / "a") == posix
    assert apply_py.find_package(tmp_path / "b") == win
    assert apply_py.find_package(tmp_path / "c") is None


def test_apply_sh_delegates_to_apply_py():
    text = (PATCHES / "apply.sh").read_text()
    assert '"$SCRIPT_DIR/apply.py"' in text
    assert "patch_" not in text.split("set -euo pipefail", 1)[1]
