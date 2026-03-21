#!/usr/bin/env bash
#
# install.sh — Set up VoiceMode local voice services for Claude Code on WSL2
#
# What this does:
#   1. Installs required apt packages (needs sudo)
#   2. Creates ~/.asoundrc for ALSA→PulseAudio routing
#   3. Registers VoiceMode MCP server in Claude Code
#   4. Adds VoiceMode permissions to Claude settings
#   5. Symlinks voicemode-switch to ~/.local/bin
#   6. Starts Docker services + proxy
#
# Prerequisites:
#   - WSL2 on Windows 11 with WSLg
#   - Docker installed and running
#   - Claude Code installed (claude CLI)
#   - uv/uvx installed (curl -LsSf https://astral.sh/uv/install.sh | sh)
#
# Usage:
#   cd ~/git/voicemode-local
#   ./install.sh [--openai-key sk-proj-...]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }
header() { echo -e "\n${BOLD}=== $1 ===${NC}"; }

OPENAI_KEY=""
for arg in "$@"; do
    case $arg in
        --openai-key=*) OPENAI_KEY="${arg#*=}" ;;
        --openai-key)   shift; OPENAI_KEY="${2:-}" ;;
    esac
done

# ─── Step 1: System packages ─────────────────────────────────────────────────
header "Step 1: System packages"

PACKAGES="libasound2-plugins libasound2-dev libportaudio2 portaudio19-dev alsa-utils sox python3-dev"
MISSING=""
for pkg in $PACKAGES; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
        MISSING="$MISSING $pkg"
    fi
done

if [ -n "$MISSING" ]; then
    echo "  Installing:$MISSING"
    sudo apt update -qq && sudo apt install -y $MISSING
    ok "Packages installed"
else
    ok "All packages already installed"
fi

# ─── Step 2: ALSA config ─────────────────────────────────────────────────────
header "Step 2: ALSA config (~/.asoundrc)"

if [ -f ~/.asoundrc ]; then
    ok "~/.asoundrc already exists"
else
    cat > ~/.asoundrc << 'EOF'
pcm.!default {
    type pulse
}
ctl.!default {
    type pulse
}
EOF
    ok "Created ~/.asoundrc"
fi

# ─── Step 3: Register VoiceMode MCP server ────────────────────────────────────
header "Step 3: VoiceMode MCP server"

if command -v claude >/dev/null 2>&1; then
    # Check if voicemode already registered
    if python3 -c "
import json, os
with open(os.path.expanduser('~/.claude.json')) as f:
    d = json.load(f)
if 'voicemode' in d.get('mcpServers', {}):
    exit(0)
exit(1)
" 2>/dev/null; then
        ok "VoiceMode MCP already registered"
    else
        claude mcp add --scope user voicemode -- uvx --refresh voice-mode
        ok "VoiceMode MCP registered"
    fi

    # Set env vars
    python3 << PYEOF
import json, os

claude_json = os.path.expanduser("~/.claude.json")
with open(claude_json) as f:
    data = json.load(f)

env = data.get("mcpServers", {}).get("voicemode", {}).get("env", {})

# Set OpenAI key if provided
openai_key = "${OPENAI_KEY}" or env.get("OPENAI_API_KEY", "")
if openai_key:
    env["OPENAI_API_KEY"] = openai_key

data["mcpServers"]["voicemode"]["env"] = env

with open(claude_json, "w") as f:
    json.dump(data, f, indent=2)
PYEOF
    ok "MCP env configured"
else
    fail "claude CLI not found — install Claude Code first"
fi

# ─── Step 4: Claude permissions ───────────────────────────────────────────────
header "Step 4: Claude permissions"

python3 << 'PYEOF'
import json, os

settings_path = os.path.expanduser("~/.claude/settings.json")
os.makedirs(os.path.dirname(settings_path), exist_ok=True)

if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)
else:
    settings = {}

perms = settings.setdefault("permissions", {})
allow = perms.setdefault("allow", [])

needed = ["mcp__voicemode__converse", "mcp__voicemode__service"]
for p in needed:
    if p not in allow:
        allow.append(p)

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)

print("  Permissions set")
PYEOF

# ─── Step 5: Symlink voicemode-switch ─────────────────────────────────────────
header "Step 5: voicemode-switch CLI"

mkdir -p ~/.local/bin
ln -sf "$SCRIPT_DIR/voicemode-switch" ~/.local/bin/voicemode-switch
ok "Symlinked to ~/.local/bin/voicemode-switch"

if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
    warn "~/.local/bin is not in PATH — add to ~/.bashrc:"
    echo '    export PATH="$HOME/.local/bin:$PATH"'
fi

# ─── Step 6: OpenAI key in bashrc ────────────────────────────────────────────
header "Step 6: Shell environment"

if [ -n "$OPENAI_KEY" ]; then
    if grep -q "OPENAI_API_KEY" ~/.bashrc 2>/dev/null; then
        # Update existing
        sed -i "s|^export OPENAI_API_KEY=.*|export OPENAI_API_KEY=\"$OPENAI_KEY\"|" ~/.bashrc
        ok "Updated OPENAI_API_KEY in ~/.bashrc"
    else
        echo "export OPENAI_API_KEY=\"$OPENAI_KEY\"" >> ~/.bashrc
        ok "Added OPENAI_API_KEY to ~/.bashrc"
    fi
else
    if grep -q "OPENAI_API_KEY" ~/.bashrc 2>/dev/null; then
        ok "OPENAI_API_KEY already in ~/.bashrc"
    else
        warn "No OpenAI key set. For openai/hybrid modes, run:"
        echo '    echo '\''export OPENAI_API_KEY="sk-..."'\'' >> ~/.bashrc'
    fi
fi

# ─── Step 7: Start services ──────────────────────────────────────────────────
header "Step 7: Start local services"

echo "  Starting Docker containers..."
cd "$SCRIPT_DIR"
docker compose up -d 2>&1 | sed 's/^/  /'

echo "  Waiting for Whisper to be ready..."
for i in $(seq 1 60); do
    if curl -sf --max-time 2 "http://127.0.0.1:9000/docs" > /dev/null 2>&1; then
        break
    fi
    sleep 1
done

echo "  Starting Whisper proxy..."
if lsof -ti:2022 > /dev/null 2>&1; then
    kill "$(lsof -ti:2022)" 2>/dev/null
    sleep 1
fi
nohup python3 "$SCRIPT_DIR/whisper-proxy.py" --port 2022 --whisper-url http://127.0.0.1:9000 \
    > /tmp/whisper-proxy.log 2>&1 &
echo $! > /tmp/whisper-proxy.pid
sleep 1

if kill -0 "$(cat /tmp/whisper-proxy.pid)" 2>/dev/null; then
    ok "All services running"
else
    fail "Proxy failed — check /tmp/whisper-proxy.log"
fi

# ─── Done ─────────────────────────────────────────────────────────────────────
header "Installation complete!"
echo ""
echo "  Next steps:"
echo "    1. Switch mode:    voicemode-switch local|hybrid|openai"
echo "    2. Restart Claude Code"
echo "    3. Use:            /voicemode:converse"
echo ""
echo "  Service management:"
echo "    voicemode-switch start    # Start Docker + proxy"
echo "    voicemode-switch stop     # Stop everything"
echo "    voicemode-switch status   # Health check"
echo ""
