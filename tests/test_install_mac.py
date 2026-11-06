"""install-mac.sh: the native macOS installer.

Runs a COPY of the script on this (Linux) machine inside a throwaway "repo"
directory and a fake HOME, with uname, brew, uv, curl, git, claude, the venv
python, patches/apply.sh and voicemode-switch all stubbed. The stubs write every
call to one log, so a test can say what was and was not done.

What this proves: the script's control flow, its questions and defaults, its
idempotency and its cleanup. What it cannot prove: that real Homebrew, uv, curl,
whisper-server or macOS behave as the stubs assume. That needs a real Mac.

Each test names the production change that would make it fail.
"""
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "install-mac.sh"
INSTALL_SH = REPO / "install.sh"

WHISPER_NAME = "ggml-large-v3-turbo-q5_0.bin"
HOMEBREW_CMD = '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'

STUB_UNAME = r'''#!/bin/bash
case "$1" in
  -s) echo "${STUB_UNAME_S:-Darwin}" ;;
  -m) echo "${STUB_UNAME_M:-arm64}" ;;
  *) echo "${STUB_UNAME_S:-Darwin}" ;;
esac
'''

STUB_BREW = r'''#!/bin/bash
echo "brew $*" >> "$STUB_LOG"
have="$STUB_STATE/brew-have"; touch "$have"
case "$1" in
  list) grep -qx "$3" "$have" ;;
  install)
    [ "${STUB_FAIL:-}" = brew-install ] && { echo "Error: stub brew install failed" >&2; exit 1; }
    shift; for p in "$@"; do echo "$p" >> "$have"; done ;;
  *) ;;
esac
'''

STUB_UV = r'''#!/bin/bash
echo "uv $*" >> "$STUB_LOG"
case "$1 $2" in
  venv*)
    # uv venv [--python 3.12] DIR  -> DIR is the last argument
    for a in "$@"; do dir="$a"; done
    mkdir -p "$dir/bin"
    cp "$STUB_PYTHON_SRC" "$dir/bin/python"; chmod +x "$dir/bin/python"
    printf '#!/bin/bash\n' > "$dir/bin/voice-mode"; chmod +x "$dir/bin/voice-mode"
    ;;
  "pip install")
    [ "${STUB_FAIL:-}" = uv-pip ] && { echo "error: stub pip failed" >&2; exit 1; }
    venv=""
    prev=""
    for a in "$@"; do
      [ "$prev" = "--python" ] && venv="$(dirname "$(dirname "$a")")"
      prev="$a"
    done
    for a in "$@"; do
      case "$a" in
        voice-mode==*) echo "${a#voice-mode==}" > "$venv/.stub_vm_version" ;;
        kokoro-onnx) touch "$venv/.stub_pkgs" ;;
      esac
    done
    ;;
esac
exit 0
'''

STUB_PYTHON = r'''#!/bin/bash
echo "python $*" >> "$STUB_LOG"
here="$(cd "$(dirname "$0")/.." && pwd)"
if [ "$1" = "-c" ]; then
  case "$2" in
    *importlib.metadata*) cat "$here/.stub_vm_version" 2>/dev/null; [ -f "$here/.stub_vm_version" ]; exit $? ;;
    *find_spec*) [ -f "$here/.stub_pkgs" ]; exit $? ;;
    *mcpServers*|*permissions*) shift; exec python3 -c "$@" ;;
  esac
  exit 0
fi
case "$*" in
  *--download-german*)
    dir="$STUB_REPO/models/kokoro-de-martin"; mkdir -p "$dir"
    if [ "${STUB_FAIL:-}" = german ]; then
      head -c 100 /dev/zero > "$dir/kokoro-martin.onnx"   # a truncated download
      echo "stub german download died" >&2; exit 1
    fi
    echo data > "$dir/kokoro-martin.onnx"; echo data > "$dir/voices-martin.npz"
    ;;
esac
exit 0
'''

STUB_CURL = r'''#!/bin/bash
out=""; prev=""
for a in "$@"; do
  [ "$prev" = "-o" ] || [ "$prev" = "--output" ] && out="$a"
  prev="$a"
done
url="${*: -1}"
echo "curl $url" >> "$STUB_LOG"
case "${STUB_FAIL:-}:$url" in
  curl-whisper:*ggml-*|curl-kokoro:*kokoro-v1.0.onnx|curl-kokoro:*voices-v1.0.bin)
    [ -n "$out" ] && head -c 100 /dev/zero > "$out"     # leave a truncated file behind
    echo "curl: (18) transfer closed" >&2; exit 18 ;;
esac
[ -n "$out" ] && echo "model-bytes" > "$out"
exit 0
'''

STUB_GIT = r'''#!/bin/bash
echo "git $*" >> "$STUB_LOG"
exit 0
'''

STUB_CLAUDE = r'''#!/bin/bash
echo "claude $*" >> "$STUB_LOG"
if [ "$1 $2" = "mcp add" ]; then
  # claude mcp add --scope user voicemode -- CMD
  python3 - "$HOME/.claude.json" "${@: -1}" <<'PY'
import json, sys
json.dump({"mcpServers": {"voicemode": {"command": sys.argv[2]}}}, open(sys.argv[1], "w"))
PY
fi
exit 0
'''

STUB_SWITCH = r'''#!/bin/bash
echo "switch $*" >> "$STUB_LOG"
[ "${STUB_FAIL:-}" = pill ] && [ "$1 $2" = "pill on" ] && { echo "pill exploded" >&2; exit 1; }
[ "${STUB_FAIL:-}" = barge ] && [ "$1 $2" = "barge on" ] && { echo "barge exploded" >&2; exit 1; }
[ "${STUB_FAIL:-}" = localonly ] && [ "$1" = localonly ] && { echo "localonly failed" >&2; exit 1; }
[ "${STUB_FAIL:-}" = health ] && [ "$1" = health ] && exit 1
exit 0
'''

STUB_APPLY = r'''#!/bin/bash
echo "apply $*" >> "$STUB_LOG"
[ "${STUB_FAIL:-}" = apply ] && { echo "[patches] ERROR stub" >&2; exit 1; }
exit 0
'''


def _write(path: Path, body: str, mode: int = 0o755) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(mode)


class Box:
    """A fake repo + HOME + stub bin, reusable across several runs."""

    def __init__(self, tmp: Path, brew: bool = True, claude: bool = True):
        self.root = tmp
        self.repo, self.home, self.bin = tmp / "repo", tmp / "home", tmp / "bin"
        self.state, self.log = tmp / "state", tmp / "calls.log"
        for d in (self.repo, self.home, self.bin, self.state):
            d.mkdir(parents=True, exist_ok=True)
        shutil.copy(INSTALLER, self.repo / "install-mac.sh")
        shutil.copy(REPO / "kokoro-onnx-server.py", self.repo / "kokoro-onnx-server.py")
        (self.repo / "install-mac.sh").chmod(0o755)
        _write(self.repo / "voicemode-switch", STUB_SWITCH)
        _write(self.repo / "voicemode-mcp", "#!/bin/bash\n")
        _write(self.repo / "patches/apply.sh", STUB_APPLY)
        _write(self.bin / "uname", STUB_UNAME)
        _write(self.bin / "uv", STUB_UV)
        _write(self.bin / "curl", STUB_CURL)
        _write(self.bin / "git", STUB_GIT)
        _write(self.state / "python-src", STUB_PYTHON)
        if brew:
            _write(self.bin / "brew", STUB_BREW)
        if claude:
            _write(self.bin / "claude", STUB_CLAUDE)

    def run(self, *args, fail: str = "", stdin: str = "", uname_s: str = "Darwin", uname_m: str = "arm64"):
        env = {
            "HOME": str(self.home),
            # Only the stubs and the system dirs: the real claude, brew and uv must be unreachable.
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "STUB_LOG": str(self.log), "STUB_STATE": str(self.state), "STUB_REPO": str(self.repo),
            "STUB_PYTHON_SRC": str(self.state / "python-src"),
            "STUB_FAIL": fail, "STUB_UNAME_S": uname_s, "STUB_UNAME_M": uname_m,
        }
        return subprocess.run([str(self.repo / "install-mac.sh"), *args], env=env, input=stdin,
                              capture_output=True, text=True, timeout=60)

    def calls(self, prefix: str = "") -> list:
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        return [l for l in lines if l.startswith(prefix)]

    @property
    def whisper(self) -> Path:
        return self.home / ".voicemode/models/whisper" / WHISPER_NAME

    @property
    def kokoro(self) -> Path:
        return self.repo / "models/kokoro"


def _box(tmp_path, **kw) -> Box:
    return Box(tmp_path, **kw)


# --- the pill: off unless asked -------------------------------------------------

def test_no_pill_by_default(tmp_path):
    # Fails if the pill question's default flips to yes, or `pill on` runs unconditionally.
    b = _box(tmp_path)
    done = b.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert not b.calls("switch pill")
    assert "Install the voice pill? [y/N]" in done.stdout


def test_yes_flag_does_not_turn_the_pill_on(tmp_path):
    # Fails if --yes is implemented as "answer yes to everything" instead of "take the defaults".
    b = _box(tmp_path)
    done = b.run("--yes")
    assert done.returncode == 0
    assert not b.calls("switch pill")
    assert "Install the voice pill?" not in done.stdout  # --yes asks nothing


def test_pill_flag_turns_the_pill_on(tmp_path):
    # Fails if --pill is ignored.
    b = _box(tmp_path)
    assert b.run("--pill", "--yes").returncode == 0
    assert b.calls("switch pill on")


def test_pill_yes_answer_turns_it_on_and_no_does_not(tmp_path):
    # Fails if the answer is not read, or any non-empty answer is taken as yes.
    b1 = _box(tmp_path / "y")
    b1.run(stdin="y\n")
    assert b1.calls("switch pill on")
    b2 = _box(tmp_path / "n")
    b2.run(stdin="n\n")
    assert not b2.calls("switch pill")


def test_a_failing_pill_warns_and_the_install_still_succeeds(tmp_path):
    # Fails if a pill failure is made fatal (set -e leaking out of the pill step).
    b = _box(tmp_path)
    done = b.run("--pill", "--yes", fail="pill")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "pill" in (done.stdout + done.stderr).lower() and "voice itself is unaffected" in done.stdout
    # voice was fully set up before the pill was attempted
    assert b.calls("switch localonly")
    assert b.whisper.exists()


# --- German: only on request ----------------------------------------------------

def test_german_is_not_downloaded_by_default_or_with_yes(tmp_path):
    # Fails if the German download becomes unconditional, or --yes implies it.
    for name, args in (("plain", ()), ("yes", ("--yes",))):
        b = _box(tmp_path / name)
        done = b.run(*args)
        assert done.returncode == 0
        assert not [c for c in b.calls("python") if "--download-german" in c], name
        assert not (b.repo / "models/kokoro-de-martin").exists(), name


def test_german_question_states_the_size_and_defaults_to_no(tmp_path):
    # Fails if the size is dropped from the question or the default becomes yes.
    b = _box(tmp_path)
    done = b.run(stdin="\n\n")
    assert "about 325 MB" in done.stdout and "[y/N]" in done.stdout.split("about 325 MB")[1][:200]
    assert not [c for c in b.calls("python") if "--download-german" in c]


def test_german_flag_downloads_it(tmp_path):
    # Fails if --german is ignored.
    b = _box(tmp_path)
    assert b.run("--german", "--yes").returncode == 0
    assert [c for c in b.calls("python") if "--download-german" in c]
    assert (b.repo / "models/kokoro-de-martin/kokoro-martin.onnx").exists()


def test_german_yes_answer_downloads_it(tmp_path):
    # Fails if the interactive answer is not honoured.
    b = _box(tmp_path)
    b.run(stdin="n\ny\n")  # pill: no, German: yes
    assert [c for c in b.calls("python") if "--download-german" in c]
    assert not b.calls("switch pill")


def test_failed_german_download_leaves_no_truncated_file_and_does_not_fail_voice(tmp_path):
    # Fails if the cleanup after a failed German download is removed (urlretrieve leaves the partial file).
    b = _box(tmp_path)
    done = b.run("--german", "--yes", fail="german")
    assert done.returncode == 0
    assert not list((b.repo / "models").glob("kokoro-de-martin/*"))
    assert "German" in done.stdout and "failed" in (done.stdout + done.stderr).lower()


# --- preconditions change nothing -----------------------------------------------

def test_missing_homebrew_stops_with_its_command_and_changes_nothing(tmp_path):
    # Fails if the Homebrew check moves after the first mutating step, or brew is installed for the user.
    b = _box(tmp_path, brew=False)
    done = b.run("--yes")
    assert done.returncode != 0
    assert HOMEBREW_CMD in done.stdout + done.stderr
    assert not b.calls()                       # no stub of any kind was called
    assert list(b.home.iterdir()) == []        # fake HOME untouched
    assert not (b.repo / ".venv").exists() and not (b.repo / "models").exists()


def test_intel_mac_is_refused_with_a_reason_and_nothing_changes(tmp_path):
    # Fails if the Apple silicon check is removed (voicemode-mcp hardcodes /opt/homebrew).
    b = _box(tmp_path)
    done = b.run("--yes", uname_m="x86_64")
    assert done.returncode != 0
    assert "Apple silicon" in done.stdout + done.stderr and "x86_64" in done.stdout + done.stderr
    assert not b.calls("brew") and list(b.home.iterdir()) == []


def test_a_non_mac_is_refused(tmp_path):
    # Fails if the Darwin check is removed.
    b = _box(tmp_path)
    done = b.run("--yes", uname_s="Linux")
    assert done.returncode != 0 and "macOS" in done.stdout + done.stderr
    assert list(b.home.iterdir()) == []


def test_help_prints_usage_and_changes_nothing(tmp_path):
    # Fails if --help falls through into the install.
    b = _box(tmp_path)
    done = b.run("--help")
    assert done.returncode == 0
    for flag in ("--yes", "--pill", "--german", "--help"):
        assert flag in done.stdout
    assert not b.calls() and list(b.home.iterdir()) == []


def test_unknown_flag_is_an_error(tmp_path):
    # Fails if unknown flags are silently ignored.
    b = _box(tmp_path)
    done = b.run("--bogus")
    assert done.returncode == 2 and not b.calls()


# --- failed required steps ------------------------------------------------------

def test_failed_required_step_exits_nonzero_and_a_rerun_continues(tmp_path):
    # Fails if a required step's failure is swallowed (|| true), or a rerun cannot get past a half-done state.
    for step in ("brew-install", "uv-pip", "apply", "curl-whisper", "curl-kokoro", "localonly"):
        b = _box(tmp_path / step)
        done = b.run("--yes", fail=step)
        assert done.returncode != 0, step
        assert "failed" in (done.stdout + done.stderr).lower(), step
        again = b.run("--yes")
        assert again.returncode == 0, (step, again.stdout + again.stderr)
        assert b.whisper.exists() and (b.kokoro / "kokoro-v1.0.onnx").exists(), step
        assert b.calls("switch localonly"), step


def test_health_failure_alone_is_a_warning(tmp_path):
    # Fails if a slow first start of the services (health not yet up) is made fatal.
    b = _box(tmp_path)
    done = b.run("--yes", fail="health")
    assert done.returncode == 0 and "health" in done.stdout.lower()


# --- idempotency ----------------------------------------------------------------

def test_rerun_skips_everything_already_present(tmp_path):
    # Fails if any existence check is dropped: a model redownloaded, a package reinstalled, brew re-run.
    b = _box(tmp_path)
    assert b.run("--yes").returncode == 0
    first = {k: len(b.calls(k)) for k in ("curl", "uv pip install", "uv venv", "brew install")}
    assert first["curl"] == 3 and first["uv venv"] == 1
    assert b.run("--yes").returncode == 0
    for k, n in first.items():
        assert len(b.calls(k)) == n, k


def test_rerun_does_not_redownload_german(tmp_path):
    # Fails if the German existence check is dropped.
    b = _box(tmp_path)
    b.run("--yes", "--german")
    n = len([c for c in b.calls("python") if "--download-german" in c])
    b.run("--yes", "--german")
    assert len([c for c in b.calls("python") if "--download-german" in c]) == n == 1


def test_existing_model_files_are_not_touched(tmp_path):
    # Fails if a present model is overwritten (e.g. by always downloading to the final path).
    b = _box(tmp_path)
    for p in (b.whisper, b.kokoro / "kokoro-v1.0.onnx", b.kokoro / "voices-v1.0.bin"):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("mine")
    assert b.run("--yes").returncode == 0
    assert not b.calls("curl")
    assert b.whisper.read_text() == "mine"


def test_rerun_keeps_user_edits_to_the_config_and_does_not_duplicate_keys(tmp_path):
    # Fails if the config is rewritten wholesale on every run.
    b = _box(tmp_path)
    b.run("--yes")
    cfg = b.home / ".voicemode-local/config"
    cfg.write_text(cfg.read_text().replace("COMPUTE_MODE=cpu", "COMPUTE_MODE=gpu") + "MINE=1\n")
    b.run("--yes")
    text = cfg.read_text()
    assert "COMPUTE_MODE=gpu\n" in text and "MINE=1\n" in text
    assert text.count("WHISPER_CPP_MODEL=") == 1


# --- downloads: never leave a truncated file ------------------------------------

def test_failed_download_leaves_no_partial_file(tmp_path):
    # Fails if curl writes straight to the final name (a rerun would then treat a truncated file as present).
    b = _box(tmp_path / "w")
    assert b.run("--yes", fail="curl-whisper").returncode != 0
    assert not b.whisper.exists() and not list(b.whisper.parent.glob("*"))
    b = _box(tmp_path / "k")
    assert b.run("--yes", fail="curl-kokoro").returncode != 0
    assert not list(b.kokoro.glob("*"))


def test_stale_partial_from_a_killed_run_is_replaced(tmp_path):
    # Fails if a leftover .part from an interrupted run is trusted or appended to.
    b = _box(tmp_path)
    b.whisper.parent.mkdir(parents=True)
    (b.whisper.parent / (WHISPER_NAME + ".part")).write_text("junk")
    assert b.run("--yes").returncode == 0
    assert b.whisper.read_text() == "model-bytes\n"
    assert not list(b.whisper.parent.glob("*.part"))


# --- what it sets up ------------------------------------------------------------

def test_config_matches_the_readme_and_points_at_the_whisper_model(tmp_path):
    # Fails if a README config key is dropped or WHISPER_CPP_MODEL points elsewhere.
    b = _box(tmp_path)
    b.run("--yes")
    text = (b.home / ".voicemode-local/config").read_text()
    for line in ("INSTALL_MODE=native", "PIPER_ENABLED=true", "KOKORO_ONNX=true",
                 "KOKORO_ONNX_PORT=8880", "COMPUTE_MODE=cpu",
                 f"WHISPER_CPP_MODEL={b.whisper}"):
        assert line + "\n" in text, line


def test_installs_the_readme_packages_with_the_pinned_voice_mode(tmp_path):
    # Fails if the pin, python 3.12, or any README package is dropped.
    b = _box(tmp_path)
    b.run("--yes")
    assert b.calls("brew install")
    brew = " ".join(b.calls("brew install"))
    for p in ("whisper-cpp", "ffmpeg", "portaudio"):
        assert p in brew
    assert any("venv" in c and "--python 3.12" in c for c in b.calls("uv"))
    pip = " ".join(b.calls("uv pip install"))
    for p in ("voice-mode==8.12.0", "kokoro-onnx", "soundfile", "numpy", "piper-tts", "pynput"):
        assert p in pip, p
    assert b.calls("apply")


def test_voice_mode_pin_matches_install_sh(tmp_path):
    # Fails if the two installers pin different upstream versions.
    pin = re.search(r'^VOICE_MODE_VERSION="([^"]+)"', INSTALL_SH.read_text(), re.M).group(1)
    assert f'VOICE_MODE_VERSION="{pin}"' in INSTALLER.read_text()


def test_model_urls_match_the_server_and_the_readme():
    # Fails if a URL here drifts from kokoro-onnx-server.py (which names the files its loader expects).
    server = (REPO / "kokoro-onnx-server.py").read_text()
    for const in ("MODEL_URL", "VOICES_URL"):
        url = re.search(rf'^{const} = "([^"]+)"', server, re.M).group(1)
        assert url in INSTALLER.read_text(), const
    whisper_url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/" + WHISPER_NAME
    text = INSTALLER.read_text()
    assert "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$WHISPER_FILE" in text
    assert f'WHISPER_FILE="{WHISPER_NAME}"' in text
    assert whisper_url in (REPO / "README.md").read_text()


def test_links_voicemode_switch_into_local_bin(tmp_path):
    # Fails if the ~/.local/bin link is dropped or points at a copy.
    b = _box(tmp_path)
    b.run("--yes")
    link = b.home / ".local/bin/voicemode-switch"
    assert link.is_symlink() and Path(os.readlink(link)) == b.repo / "voicemode-switch"


def test_registers_the_mcp_wrapper_once(tmp_path):
    # Fails if the raw voice-mode binary is registered instead of the voicemode-mcp wrapper
    # (no PTT listener, no service ensure), or a rerun re-registers.
    b = _box(tmp_path)
    b.run("--yes")
    adds = b.calls("claude mcp add")
    assert adds == [f"claude mcp add --scope user voicemode -- {b.repo}/voicemode-mcp"]
    b.run("--yes")
    assert len(b.calls("claude mcp add")) == 1
    settings = json.loads((b.home / ".claude/settings.json").read_text())
    assert "mcp__voicemode__converse" in settings["permissions"]["allow"]


def test_without_claude_it_warns_with_the_command_and_continues(tmp_path):
    # Fails if a missing claude CLI aborts the install.
    b = _box(tmp_path, claude=False)
    done = b.run("--yes")
    assert done.returncode == 0
    assert "claude mcp add --scope user voicemode" in done.stdout


def test_summary_lists_the_manual_macos_permissions(tmp_path):
    # Fails if the closing checklist loses the permissions that make PTT work.
    b = _box(tmp_path)
    done = b.run("--yes")
    for word in ("Accessibility", "Input Monitoring", "Microphone", "VOICEMODE_CONTROL_CHANNEL_ENABLED"):
        assert word in done.stdout, word


def test_never_installs_homebrew_or_touches_git(tmp_path):
    # Fails if the script starts fetching/running the Homebrew installer or doing git work.
    b = _box(tmp_path)
    b.run("--yes")
    assert not b.calls("git")
    assert not [c for c in b.calls("curl") if "Homebrew" in c]


# --- install.sh hands off on macOS ----------------------------------------------

def _install_sh_head(tmp_path: Path) -> Path:
    """install.sh cut just before its first real work, plus a marker. The Linux path is never run:
    it would run apt and kill live services on whatever machine runs the tests."""
    text = INSTALL_SH.read_text()
    cut = text.index("# ─── Install mode selection")
    d = tmp_path / "repo"
    d.mkdir(parents=True)
    (d / "install.sh").write_text(text[:cut] + 'echo REACHED_LINUX_PATH\n')
    (d / "install.sh").chmod(0o755)
    _write(d / "install-mac.sh", '#!/bin/bash\necho "HANDED_OFF $*"\n')
    bin_dir = tmp_path / "bin"
    _write(bin_dir / "uname", STUB_UNAME)
    return d / "install.sh"


def _run_install_sh(tmp_path, uname_s, *args):
    script = _install_sh_head(tmp_path)
    env = {"HOME": str(tmp_path / "home"), "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "STUB_UNAME_S": uname_s}
    return subprocess.run([str(script), *args], env=env, capture_output=True, text=True, timeout=30)


def test_install_sh_hands_off_to_install_mac_on_darwin_with_its_arguments(tmp_path):
    # Fails if the Darwin handoff is removed, does not exec, or drops the arguments.
    done = _run_install_sh(tmp_path, "Darwin", "--yes", "--german")
    assert "HANDED_OFF --yes --german" in done.stdout and "REACHED_LINUX_PATH" not in done.stdout


def test_install_sh_does_not_hand_off_on_linux(tmp_path):
    # Fails if the handoff is unconditional.
    done = _run_install_sh(tmp_path, "Linux", "--yes")
    assert "REACHED_LINUX_PATH" in done.stdout and "HANDED_OFF" not in done.stdout


def test_install_sh_handoff_comes_before_any_work():
    # Fails if the handoff moves below the first prompt or mkdir (those must not run on a Mac first).
    text = INSTALL_SH.read_text()
    hand = text.index("install-mac.sh")
    for marker in ("read -r", 'mkdir -p "$CONFIG_DIR"', "sudo apt"):
        assert hand < text.index(marker), marker


# --- bash 3.2 (macOS /bin/bash) -------------------------------------------------

BASH4_ONLY = [
    (r"declare\s+-[a-zA-Z]*A", "associative array"),
    (r"local\s+-[a-zA-Z]*A", "associative array"),
    (r"\bmapfile\b|\breadarray\b", "mapfile/readarray"),
    (r"\$\{[A-Za-z_][A-Za-z0-9_]*(,,|\^\^|,|\^)\}", "case-conversion expansion"),
    (r"\$\{[A-Za-z_][A-Za-z0-9_]*(,,|\^\^)[^}]*\}", "case-conversion expansion"),
    (r"\|&", "|& pipe"),
    (r"&>>", "&>> redirect"),
    (r"\bcoproc\b", "coproc"),
    (r"\[\[\s+-v\s", "[[ -v ]]"),
    (r"\bread\b[^\n]*\s-i\s", "read -i"),
    (r";;&|;&", "case fallthrough"),
    (r"\breadlink\s+-f\b", "readlink -f (GNU only, absent on macOS)"),
    (r"\bsed\s+-i\b", "sed -i (differs on BSD sed)"),
    (r"\bstat\s+-c\b", "stat -c (GNU only)"),
    (r"\bdate\s+-d\b", "date -d (GNU only)"),
    (r"\bgrep\s+-[a-zA-Z]*P", "grep -P (absent on macOS)"),
    (r"shopt\s+-s\s+globstar", "globstar"),
    (r"\$\{[A-Za-z_]+\[@\]\}", "array expansion (breaks under set -u on bash 3.2 when empty)"),
]


def test_script_uses_no_bash4_only_or_gnu_only_syntax():
    # Fails if someone introduces bash 4 / GNU-only syntax macOS's bash 3.2 and BSD tools cannot run.
    code = "\n".join(l for l in INSTALLER.read_text().splitlines() if not l.lstrip().startswith("#"))
    hits = [(why, pat) for pat, why in BASH4_ONLY if re.search(pat, code)]
    assert not hits, hits


def test_script_has_a_bash_shebang_and_is_executable_and_parses():
    # Fails if the shebang is /bin/sh, the exec bit is lost, or the syntax is broken.
    assert INSTALLER.read_text().startswith("#!/usr/bin/env bash\n") or INSTALLER.read_text().startswith("#!/bin/bash\n")
    assert INSTALLER.stat().st_mode & stat.S_IXUSR
    assert subprocess.run(["bash", "-n", str(INSTALLER)]).returncode == 0
    assert subprocess.run(["bash", "-n", str(INSTALL_SH)]).returncode == 0


# --- found on a real Mac (FELIX-MAC, 2026-11-05, throwaway HOME) ------------------

STUB_COPILOT_EXISTS = r'''#!/bin/bash
echo "copilot $*" >> "$STUB_LOG"
case "$1 $2" in
  "mcp list") echo "voicemode (stdio)";;
  "mcp add") echo 'Error: Server "voicemode" already exists' >&2; exit 1;;
esac
'''


def test_a_copilot_entry_that_already_exists_is_not_a_warning(tmp_path):
    # Fails if a re-run warns "Could not register" because copilot's list output does not
    # contain the wrapper path (seen on every re-run on a real Mac).
    b = _box(tmp_path)
    _write(b.bin / "copilot", STUB_COPILOT_EXISTS)
    done = b.run("--yes")
    assert done.returncode == 0
    assert "Could not register voicemode in GitHub Copilot CLI" not in done.stdout + done.stderr
    assert "GitHub Copilot CLI already has voicemode" in done.stdout


def test_the_whisper_download_states_its_measured_size(tmp_path):
    # Fails if the size goes back to "not recorded" (measured 560 MB on a real Mac).
    done = _box(tmp_path).run("--yes")
    assert "about 560 MB" in done.stdout


def test_not_asking_for_the_pill_does_not_claim_it_is_absent(tmp_path):
    # Fails if a run without --pill says "not installed" when an earlier run installed it.
    done = _box(tmp_path).run("--yes")
    assert "Voice pill: not installed" not in done.stdout
    assert "Voice pill: not requested" in done.stdout


# --- --barge: interrupt by voice, only on request --------------------------------

BARGE_LINE = ("Interrupt by voice works with headphones; on the built-in speakers it "
              "interrupts itself until echo cancellation lands.")


def test_barge_is_not_turned_on_by_default_or_with_yes(tmp_path):
    # Fails if barge becomes default-on, or --yes implies it.
    b = _box(tmp_path)
    done = b.run("--yes")
    assert done.returncode == 0
    assert not b.calls("switch barge") and BARGE_LINE not in done.stdout


def test_barge_flag_turns_it_on_after_install_and_prints_the_headphones_line(tmp_path):
    # Fails if --barge is ignored, runs before the services exist, or the warning is missing.
    b = _box(tmp_path)
    done = b.run("--barge", "--yes")
    assert done.returncode == 0, done.stdout + done.stderr
    assert b.calls("switch barge on")
    assert BARGE_LINE in done.stdout
    assert b.calls().index(b.calls("switch barge on")[0]) > b.calls().index(b.calls("switch localonly")[0])


def test_a_failing_barge_warns_and_the_install_still_succeeds(tmp_path):
    # Fails if a barge failure is made fatal.
    b = _box(tmp_path)
    done = b.run("--barge", "--yes", fail="barge")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "barge" in (done.stdout + done.stderr).lower()
    assert BARGE_LINE not in done.stdout


def test_help_lists_barge(tmp_path):
    assert "--barge" in _box(tmp_path).run("--help").stdout
