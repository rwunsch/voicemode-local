#!/bin/bash
# Run the Windows-only tests on real Windows, from WSL:
#   tests/test_control_transport_windows.py  the named-pipe transport
#   tests/test_voice_pill_windows.py         pill backend: liveness, lock, stdin, files
#   tests/test_apply_windows.py              patches/apply.py on pristine copies, compared
#                                            byte-for-byte with what Linux's apply.py made
#
# Copies the modules they load and the test files into a fresh folder under the
# Windows temp dir (same patches/ + tests/ layout as the repo), runs pytest there
# with Windows Python, and deletes the folder on exit. No audio. The pill tests
# keep all state in pytest's tmp_path (VOICEMODE_BASE_DIR), never ~/.voicemode.
#
# Usage: tests/run_windows_transport_tests.sh [extra pytest args]
#   VML_WIN_PY overrides the interpreter (default: "py -3.14"); it needs pytest.
#   VM812_SRC, the pristine 8.12.0 sources, enables the apply.py check; without it
#   that check skips.
set -euo pipefail

REPO="$(dirname "$(dirname "$(readlink -f "$0")")")"
WIN_PY="${VML_WIN_PY:-py -3.14}"

WIN_TEMP="$(powershell.exe -NoProfile -Command '[IO.Path]::GetTempPath()' | tr -d '\r')"
DIR="$(wslpath -u "$WIN_TEMP")vml-transport-tests-$$-$(date +%s)"
mkdir -p "$DIR/patches" "$DIR/tests"
cleanup() { rm -rf "$DIR"; }
trap cleanup EXIT

# The whole patch set: apply.py needs every script and module the manifest lists.
cp "$REPO/patches/"*.py "$REPO/patches/manifest.json" "$DIR/patches/"
APPLY_ENV=""
if [ -n "${VM812_SRC:-}" ]; then
    mkdir -p "$DIR/vm812" "$DIR/expected/voice_mode/tools"
    for f in control_channel.py control_socket.py core.py simple_failover.py streaming.py converse.py; do
        cp "$VM812_SRC/$f" "$DIR/vm812/$f"
    done
    for f in control_channel.py control_socket.py core.py simple_failover.py streaming.py; do
        cp "$DIR/vm812/$f" "$DIR/expected/voice_mode/$f"
    done
    cp "$DIR/vm812/converse.py" "$DIR/expected/voice_mode/tools/converse.py"
    python3 "$REPO/patches/apply.py" --package-dir "$DIR/expected/voice_mode" >/dev/null
    APPLY_ENV="\$env:VML_APPLY_SRC='$(wslpath -w "$DIR/vm812")'; \$env:VML_APPLY_EXPECTED='$(wslpath -w "$DIR/expected/voice_mode")'; "
fi
TESTS_WIN=""
for t in test_control_transport_windows.py test_voice_pill_windows.py test_apply_windows.py; do
    cp "$REPO/tests/$t" "$DIR/tests/"
    TESTS_WIN="$TESTS_WIN '$(wslpath -w "$DIR/tests/$t")'"
done
BASETEMP_WIN="$(wslpath -w "$DIR")\\tmp"   # pytest's tmp_path stays inside DIR

echo "[windows-transport] running in $(wslpath -w "$DIR")"
set +e
powershell.exe -NoProfile -Command \
    "\$env:PYTHONDONTWRITEBYTECODE='1'; $APPLY_ENV$WIN_PY -m pytest -q -p no:cacheprovider -rs --rootdir='$(wslpath -w "$DIR")' --basetemp='$BASETEMP_WIN' $TESTS_WIN $*; exit \$LASTEXITCODE"
rc=$?
set -e
echo "[windows-transport] exit $rc; removing $(wslpath -w "$DIR")"
exit $rc
