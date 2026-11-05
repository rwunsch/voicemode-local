#!/usr/bin/env bash
#
# install-mac.sh — Set up VoiceMode local voice services natively on macOS
#
# This is the README's "macOS (native, no Docker)" section as a script:
# whisper.cpp (Metal) for speech to text, Kokoro ONNX and Piper for text to
# speech, the pinned voice-mode in a local venv with this repo's patches, and
# the MCP server registered in Claude Code. No Docker, no sudo.
#
# Needs: Apple silicon, Homebrew (never installed for you), macOS /bin/bash 3.2.
# Safe to re-run: whatever is already present is skipped. If a step fails, fix
# the cause and run it again; it continues where it stopped.
#
# Usage: ./install-mac.sh [--yes] [--pill] [--german] [--help]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

ok()     { echo -e "  ${GREEN}✓${NC} $1"; }
fail()   { echo -e "  ${RED}✗${NC} $1" >&2; }
warn()   { echo -e "  ${YELLOW}!${NC} $1"; }
header() { echo -e "\n${BOLD}=== $1 ===${NC}"; }

# Pinned: our patch anchors (patches/*.py, see patches/apply.sh) target this exact
# upstream version. Same pin as install.sh. Bump deliberately and re-verify the anchors.
VOICE_MODE_VERSION="8.12.0"

HOMEBREW_INSTALL_CMD='/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'

# Sources: whisper URL from README.md (macOS section); Kokoro URLs are the
# constants in kokoro-onnx-server.py (which loads exactly these file names).
WHISPER_FILE="ggml-large-v3-turbo-q5_0.bin"
WHISPER_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$WHISPER_FILE"
KOKORO_MODEL_URL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
KOKORO_VOICES_URL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"

usage() {
    cat <<'EOF'
Usage: ./install-mac.sh [--yes] [--pill] [--german] [--help]

Native macOS setup for voicemode-local (Apple silicon, Homebrew, no Docker).

  --yes      Unattended: ask nothing, take every default.
             (Defaults: no voice pill, no German voice.)
  --pill     Install the voice pill (off by default; a failure only warns).
  --german   Also download the German voice dm_martin (about 325 MB).
  --help     Show this text.

Safe to re-run: what is already installed or downloaded is skipped.
EOF
}

ASSUME_YES=0
WANT_PILL=0
WANT_GERMAN=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --yes|-y)  ASSUME_YES=1 ;;
        --pill)    WANT_PILL=1 ;;
        --german)  WANT_GERMAN=1 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

CURRENT_PART=""
cleanup_partial() {
    [ -n "$CURRENT_PART" ] && rm -f "$CURRENT_PART"
    return 0
}
trap cleanup_partial EXIT
trap 'cleanup_partial; exit 130' INT TERM

die() {
    fail "$1"
    echo "  Fix the cause above and run ./install-mac.sh again; finished steps are skipped." >&2
    exit 1
}

# ask_yes "prompt including [y/N]" -> 0 only for an explicit yes. Empty answer or EOF = no.
ask_yes() {
    local ans=""
    printf '  %s ' "$1"
    read -r ans || ans=""
    case "$ans" in
        y|Y|yes|Yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

# download URL DEST — to DEST.part first, moved into place only when curl succeeded,
# so a killed or failed download never leaves a truncated file under the real name.
download() {
    local url="$1" dest="$2" part="$2.part"
    mkdir -p "$(dirname "$dest")"
    rm -f "$part"
    CURRENT_PART="$part"
    if curl -fL --retry 2 --progress-bar -o "$part" "$url" && [ -s "$part" ]; then
        mv -f "$part" "$dest"
        CURRENT_PART=""
        return 0
    fi
    rm -f "$part"
    CURRENT_PART=""
    return 1
}

# ─── Preflight: nothing is changed until all of this passes ─────────────────
header "Preflight"

if [ "$(uname -s)" != "Darwin" ]; then
    fail "This installer is for macOS. On Linux/WSL use ./install.sh."
    exit 1
fi
ARCH="$(uname -m)"
if [ "$ARCH" != "arm64" ]; then
    fail "Unsupported: this Mac reports $ARCH. Only Apple silicon (arm64) is supported."
    echo "  Homebrew would live in /usr/local on Intel, which voicemode-mcp does not put on PATH," >&2
    echo "  and nothing here has been tried there." >&2
    exit 1
fi
ok "Apple silicon ($ARCH)"

# GUI-launched shells may not have Homebrew on PATH (voicemode-mcp does the same).
if ! command -v brew >/dev/null 2>&1 && [ -x /opt/homebrew/bin/brew ]; then
    PATH="/opt/homebrew/bin:$PATH"
    export PATH
fi
if ! command -v brew >/dev/null 2>&1; then
    fail "Homebrew is not installed, and this script never installs it for you."
    echo "  Install it with:" >&2
    echo "    $HOMEBREW_INSTALL_CMD" >&2
    echo "  then open a new terminal and run ./install-mac.sh again. Nothing has been changed." >&2
    exit 1
fi
ok "Homebrew found"

# ─── Questions (asked once, up front) ───────────────────────────────────────
if [ "$ASSUME_YES" = "0" ]; then
    if [ "$WANT_PILL" = "0" ]; then
        echo ""
        echo "  The voice pill is a small always-on-top window showing who is talking (off by default)."
        if ask_yes "Install the voice pill? [y/N]"; then WANT_PILL=1; fi
    fi
    if [ "$WANT_GERMAN" = "0" ]; then
        echo ""
        if ask_yes "Download the German voice dm_martin (about 325 MB)? [y/N]"; then WANT_GERMAN=1; fi
    fi
fi
[ "$WANT_PILL" = "1" ] && ok "Voice pill: will install" || ok "Voice pill: not installed (later: voicemode-switch pill on)"
[ "$WANT_GERMAN" = "1" ] && ok "German voice: will download" || ok "German voice: not downloaded"

# ─── Step 1: Homebrew packages ──────────────────────────────────────────────
header "Step 1: Homebrew packages (whisper-cpp ffmpeg portaudio)"

MISSING=""
for pkg in whisper-cpp ffmpeg portaudio; do
    if ! brew list --versions "$pkg" >/dev/null 2>&1; then
        MISSING="$MISSING $pkg"
    fi
done
if [ -n "$MISSING" ]; then
    echo "  Installing:$MISSING"
    # shellcheck disable=SC2086
    brew install $MISSING || die "brew install failed for:$MISSING"
    ok "Installed:$MISSING"
else
    ok "All already installed"
fi

# ─── Step 2: uv and the Python 3.12 venv ────────────────────────────────────
header "Step 2: uv and the Python 3.12 venv"

if ! command -v uv >/dev/null 2>&1; then
    echo "  uv not found; installing it with Homebrew..."
    brew install uv || die "brew install uv failed"
fi
ok "uv found"

VENV_DIR="$SCRIPT_DIR/.venv"
if [ -x "$VENV_DIR/bin/python" ]; then
    ok "venv already present ($VENV_DIR)"
else
    uv venv --python 3.12 "$VENV_DIR" || die "Creating the venv with Python 3.12 failed"
    ok "venv created ($VENV_DIR)"
fi

# ─── Step 3: Python packages ────────────────────────────────────────────────
header "Step 3: Python packages (voice-mode==$VOICE_MODE_VERSION and friends)"

HAVE_VM="$("$VENV_DIR/bin/python" -c "import importlib.metadata as m; print(m.version('voice-mode'))" 2>/dev/null || true)"
if [ "$HAVE_VM" = "$VOICE_MODE_VERSION" ] && "$VENV_DIR/bin/python" -c "import importlib.util as u, sys; sys.exit(any(u.find_spec(m) is None for m in ('kokoro_onnx', 'soundfile', 'numpy', 'piper', 'pynput')))" 2>/dev/null; then
    ok "voice-mode $VOICE_MODE_VERSION and the other packages already installed"
else
    uv pip install --python "$VENV_DIR/bin/python" "voice-mode==$VOICE_MODE_VERSION" \
        kokoro-onnx soundfile numpy piper-tts pynput \
        || die "pip install into the venv failed"
    ok "Packages installed"
fi

# ─── Step 4: Patches ────────────────────────────────────────────────────────
header "Step 4: voicemode-local patches"

"$SCRIPT_DIR/patches/apply.sh" || die "Applying the patches failed (patches/apply.sh)"
ok "Patches applied"

# ─── Step 5: Models ─────────────────────────────────────────────────────────
header "Step 5: Models"

WHISPER_DIR="$HOME/.voicemode/models/whisper"
WHISPER_MODEL="$WHISPER_DIR/$WHISPER_FILE"
if [ -s "$WHISPER_MODEL" ]; then
    ok "Whisper model present ($WHISPER_FILE)"
else
    echo "  Downloading the Whisper model $WHISPER_FILE (size not recorded in this repo)..."
    download "$WHISPER_URL" "$WHISPER_MODEL" || die "Downloading the Whisper model failed (no partial file kept)"
    ok "Whisper model downloaded"
fi

KOKORO_DIR="$SCRIPT_DIR/models/kokoro"
mkdir -p "$KOKORO_DIR" "$SCRIPT_DIR/models/piper"
if [ -s "$KOKORO_DIR/kokoro-v1.0.onnx" ] && [ -s "$KOKORO_DIR/voices-v1.0.bin" ]; then
    ok "Kokoro model and voices present"
else
    echo "  Downloading the Kokoro model and voices (about 338 MB together)..."
    [ -s "$KOKORO_DIR/kokoro-v1.0.onnx" ] || download "$KOKORO_MODEL_URL" "$KOKORO_DIR/kokoro-v1.0.onnx" \
        || die "Downloading the Kokoro model failed (no partial file kept)"
    [ -s "$KOKORO_DIR/voices-v1.0.bin" ] || download "$KOKORO_VOICES_URL" "$KOKORO_DIR/voices-v1.0.bin" \
        || die "Downloading the Kokoro voices failed (no partial file kept)"
    ok "Kokoro model and voices downloaded"
fi

GERMAN_FAILED=0
if [ "$WANT_GERMAN" = "1" ]; then
    DE_DIR="$SCRIPT_DIR/models/kokoro-de-martin"
    if [ -s "$DE_DIR/kokoro-martin.onnx" ] && [ -s "$DE_DIR/voices-martin.npz" ]; then
        ok "German model present (voice dm_martin)"
    else
        echo "  Downloading the German Kokoro model (about 325 MB)..."
        # kokoro-onnx-server.py writes straight to the final name, so a failure can leave a
        # truncated file; remove anything this attempt created.
        de_had_model=0; de_had_voices=0
        [ -e "$DE_DIR/kokoro-martin.onnx" ] && de_had_model=1
        [ -e "$DE_DIR/voices-martin.npz" ] && de_had_voices=1
        if "$VENV_DIR/bin/python" "$SCRIPT_DIR/kokoro-onnx-server.py" --model-dir "$KOKORO_DIR" --download-german \
            && [ -s "$DE_DIR/kokoro-martin.onnx" ] && [ -s "$DE_DIR/voices-martin.npz" ]; then
            ok "German model ready (voice dm_martin)"
        else
            [ "$de_had_model" = "0" ] && rm -f "$DE_DIR/kokoro-martin.onnx"
            [ "$de_had_voices" = "0" ] && rm -f "$DE_DIR/voices-martin.npz"
            rmdir "$DE_DIR" 2>/dev/null || true
            GERMAN_FAILED=1
            warn "German model download failed (no partial file kept). English voices are unaffected."
        fi
    fi
fi

# ─── Step 6: Config ─────────────────────────────────────────────────────────
header "Step 6: ~/.voicemode-local/config"

CONFIG_DIR="$HOME/.voicemode-local"
CONFIG_FILE="$CONFIG_DIR/config"
mkdir -p "$CONFIG_DIR"
touch "$CONFIG_FILE"
# Add a key only when absent, so a re-run keeps what the user changed.
config_default() {
    if ! grep -q "^$1=" "$CONFIG_FILE" 2>/dev/null; then
        printf '%s=%s\n' "$1" "$2" >> "$CONFIG_FILE"
    fi
}
config_default INSTALL_MODE native
config_default PIPER_ENABLED true
config_default KOKORO_ONNX true
config_default KOKORO_ONNX_PORT 8880
config_default COMPUTE_MODE cpu
config_default WHISPER_CPP_MODEL "$WHISPER_MODEL"
ok "Config in place ($CONFIG_FILE)"

# ─── Step 7: voicemode-switch CLI and services ──────────────────────────────
header "Step 7: voicemode-switch and local services"

mkdir -p "$HOME/.local/bin"
ln -sf "$SCRIPT_DIR/voicemode-switch" "$HOME/.local/bin/voicemode-switch"
ok "Linked ~/.local/bin/voicemode-switch"
NEED_PATH=0
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) NEED_PATH=1; warn "~/.local/bin is not on your PATH (see the list at the end)" ;;
esac

"$SCRIPT_DIR/voicemode-switch" localonly || die "voicemode-switch localonly failed"
if ! "$SCRIPT_DIR/voicemode-switch" ensure; then
    warn "voicemode-switch ensure reported a problem; the MCP launcher retries it on every session start."
fi
if "$SCRIPT_DIR/voicemode-switch" health; then
    ok "Services healthy"
else
    warn "Not every service is up yet (models can take a while to load). Check later with: voicemode-switch health"
fi

# ─── Step 8: Register the MCP server ────────────────────────────────────────
header "Step 8: VoiceMode MCP server"

# Registers the voicemode-mcp wrapper (as README.md and install.ps1 do), not the raw
# voice-mode binary (as install.sh does): the wrapper starts the services and the
# push-to-talk listener on every session.
MCP_BIN="$SCRIPT_DIR/voicemode-mcp"
MCP_ADD_CMD="claude mcp add --scope user voicemode -- \"$MCP_BIN\""
CLAUDE_REGISTERED=0
if command -v claude >/dev/null 2>&1; then
    if "$VENV_DIR/bin/python" -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
sys.exit(0 if d.get('mcpServers', {}).get('voicemode', {}).get('command') == sys.argv[2] else 1)
" "$HOME/.claude.json" "$MCP_BIN" 2>/dev/null; then
        ok "VoiceMode MCP already registered"
    else
        claude mcp remove voicemode >/dev/null 2>&1 || true
        claude mcp add --scope user voicemode -- "$MCP_BIN" || die "claude mcp add failed"
        ok "VoiceMode MCP registered in Claude Code"
    fi
    CLAUDE_REGISTERED=1

    # Same permissions install.sh adds.
    if "$VENV_DIR/bin/python" -c "
import json, os, sys
path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
settings = json.load(open(path)) if os.path.exists(path) else {}
allow = settings.setdefault('permissions', {}).setdefault('allow', [])
for p in ('mcp__voicemode__converse', 'mcp__voicemode__service', 'mcp__voicemode__switch_mode'):
    if p not in allow:
        allow.append(p)
json.dump(settings, open(path, 'w'), indent=2)
" "$HOME/.claude/settings.json" 2>/dev/null; then
        ok "Claude permissions set"
    else
        warn "Could not update ~/.claude/settings.json; approve the voicemode tools when Claude asks"
    fi
else
    warn "claude CLI not found. Install Claude Code, then run: $MCP_ADD_CMD"
fi

COPILOT_NOTE=""
if command -v copilot >/dev/null 2>&1; then
    if copilot mcp list 2>/dev/null | grep -q "$MCP_BIN"; then
        ok "GitHub Copilot CLI already has voicemode"
    elif copilot mcp add voicemode -- "$MCP_BIN"; then
        ok "Registered voicemode in GitHub Copilot CLI"
    else
        warn "Could not register voicemode in GitHub Copilot CLI: copilot mcp add voicemode -- \"$MCP_BIN\""
    fi
fi

# ─── Step 9: Voice pill (only on request) ───────────────────────────────────
PILL_FAILED=0
if [ "$WANT_PILL" = "1" ]; then
    header "Step 9: Voice pill"
    # A pill problem must never fail voice: warn and carry on.
    if "$SCRIPT_DIR/voicemode-switch" pill on; then
        ok "Voice pill on"
    else
        PILL_FAILED=1
        warn "The voice pill did not start (the voice itself is unaffected). Try again later with: voicemode-switch pill on"
    fi
fi

# ─── Done ───────────────────────────────────────────────────────────────────
header "Installation finished"
echo ""
echo "  Still manual:"
echo "    1. Restart Claude Code (or /mcp reconnect), then use /voicemode:converse."
echo "    2. System Settings > Privacy & Security, for your terminal app:"
echo "         Microphone         macOS asks on first use; allow it"
echo "         Accessibility      needed for push-to-talk"
echo "         Input Monitoring   needed for push-to-talk; quit and reopen the terminal afterwards"
echo "       Without the last two the push-to-talk keys are silently inert."
echo "       Details: docs/macos-push-to-talk/README.md"
if [ "$WANT_PILL" = "0" ] || [ "$PILL_FAILED" = "1" ]; then
    echo "    3. Push-to-talk needs the control channel on (the voice pill sets it for you):"
    echo "         echo 'VOICEMODE_CONTROL_CHANNEL_ENABLED=true' >> ~/.voicemode/voicemode.env"
fi
if [ "$NEED_PATH" = "1" ]; then
    echo "    4. Put the CLI on your PATH (zsh):"
    echo "         echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
fi
if [ "$CLAUDE_REGISTERED" = "0" ]; then
    echo "    5. Register the MCP server once Claude Code is installed:"
    echo "         $MCP_ADD_CMD"
fi
echo "    -  VS Code Copilot Chat: add voicemode-mcp to ~/Library/Application Support/Code/User/mcp.json (README, macOS section)."
if [ "$WANT_GERMAN" = "0" ]; then
    echo "    -  German voice (about 325 MB), whenever you want it: ./install-mac.sh --german"
fi
if [ "$GERMAN_FAILED" = "1" ]; then
    echo "    -  German voice download failed: run ./install-mac.sh --german again."
fi
if [ "$WANT_PILL" = "0" ]; then
    echo "    -  Voice pill, whenever you want it: voicemode-switch pill on"
fi
echo ""
echo "  Check services any time:  voicemode-switch health"
echo ""
