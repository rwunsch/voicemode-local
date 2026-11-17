#!/usr/bin/env bash
# ports.sh — free a port for install.sh without killing what is not ours.
#
# Sourced by install.sh after SCRIPT_DIR is set. Provides:
#   - vm_free_port PORT OWN_RE WORD : 0 when PORT is free or was freed, 1 otherwise
#   - vm_kokoro_own_re PORT         : the command line of this install's Kokoro ONNX server
#   - vm_stt_own_re PORT            : ... of its faster-whisper server or whisper-proxy
#
# install.sh used to `kill "$(lsof -ti:PORT)"`: every listener, whatever it was. Now, with the
# same rules as voicemode-switch:
#   this install's own earlier server   stopped by exact pid (a process of the current user
#                                       whose command line matches OWN_RE, anchored to
#                                       SCRIPT_DIR and --port PORT)
#   a Docker container publishing PORT  `docker stop <its name>` when its name or image
#   whose name or image contains WORD   contains WORD (kokoro / whisper)
#   anything else                       one line naming it on stderr, return 1, nothing killed

# $SCRIPT_DIR with every regex metacharacter escaped.
vm_script_dir_regex() { printf '%s' "$SCRIPT_DIR" | sed 's/[][\.*^$(){}?+|/]/\\&/g'; }

vm_kokoro_own_re() {
    local d; d="$(vm_script_dir_regex)"
    printf '%s' "([^ ]*python[0-9.]* $d/kokoro-onnx-server\\.py|$d/scripts/kokoro-(gpu|cpu)-start\\.sh) .*--port $1( |\$)"
}

vm_stt_own_re() {
    local d; d="$(vm_script_dir_regex)"
    printf '%s' "([^ ]*python[0-9.]* $d/(faster-whisper-server|whisper-proxy)\\.py|$d/scripts/faster-whisper-start\\.sh) .*--port $1( |\$)"
}

vm_free_port() {
    local port="$1" own_re="$2" word="$3" pids own pid n name image other="" first
    pids="$(lsof -ti:"$port" 2>/dev/null | tr '\n' ' ' | sed 's/ $//')" || pids=""
    if [ -z "$pids" ] && ! curl -s --max-time 2 -o /dev/null "http://127.0.0.1:$port/" 2>/dev/null; then
        return 0
    fi
    own="$(ps -u "$(id -u)" -ww -o pid=,args= 2>/dev/null | grep -E "^ *[0-9]+ $own_re" \
        | awk '{print $1}' | sort -un | tr '\n' ' ' | sed 's/ $//')" || own=""
    if [ -n "$own" ]; then
        echo "  $port is held by this install's earlier server (PID $own): stopping it"
        for pid in $own; do kill "$pid" 2>/dev/null || true; done
        for pid in $own; do
            n=0
            while kill -0 "$pid" 2>/dev/null; do
                n=$((n + 1))
                if [ "$n" -gt 50 ]; then
                    echo "  $port: PID $pid did not stop" >&2
                    return 1
                fi
                sleep 0.1
            done
        done
        return 0
    fi
    if command -v docker > /dev/null 2>&1; then
        while IFS=$'\t' read -r name image; do
            [ -n "$name" ] || continue
            case "$name $image" in
                *"$word"*)
                    echo "  $port is held by the Docker container $name ($image): stopping it"
                    if docker stop "$name" > /dev/null 2>&1; then return 0; fi
                    echo "  $port: docker stop $name failed" >&2
                    return 1 ;;
            esac
            other="the Docker container $name ($image)"
        done < <(docker ps --filter "publish=$port" --format '{{.Names}}{{"\t"}}{{.Image}}' 2>/dev/null || true)
    fi
    if [ -z "$other" ] && [ -n "$pids" ]; then
        first="${pids%% *}"
        other="PID $pids ($(ps -o comm= -p "$first" 2>/dev/null || echo unknown))"
    fi
    echo "  $port is held by ${other:-an unidentified service}, not by this install; not stopping it (free the port and run install.sh again)" >&2
    return 1
}
