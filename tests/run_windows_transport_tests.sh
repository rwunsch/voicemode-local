#!/bin/bash
# Run the Windows-only tests on real Windows, from WSL:
#   tests/test_control_transport_windows.py  the named-pipe transport
#   tests/test_voice_pill_windows.py         pill backend: liveness, lock, stdin, files
#
# Copies the modules they load and the test files into a fresh folder under the
# Windows temp dir (same patches/ + tests/ layout as the repo), runs pytest there
# with Windows Python, and deletes the folder on exit. No audio. The pill tests
# keep all state in pytest's tmp_path (VOICEMODE_BASE_DIR), never ~/.voicemode.
#
# Usage: tests/run_windows_transport_tests.sh [extra pytest args]
#   VML_WIN_PY overrides the interpreter (default: "py -3.14"); it needs pytest.
set -euo pipefail

REPO="$(dirname "$(dirname "$(readlink -f "$0")")")"
WIN_PY="${VML_WIN_PY:-py -3.14}"

WIN_TEMP="$(powershell.exe -NoProfile -Command '[IO.Path]::GetTempPath()' | tr -d '\r')"
DIR="$(wslpath -u "$WIN_TEMP")vml-transport-tests-$$-$(date +%s)"
mkdir -p "$DIR/patches" "$DIR/tests"
cleanup() { rm -rf "$DIR"; }
trap cleanup EXIT

for m in control_transport.py voice_pill_state.py voice_pill.py; do
    cp "$REPO/patches/$m" "$DIR/patches/"
done
TESTS_WIN=""
for t in test_control_transport_windows.py test_voice_pill_windows.py; do
    cp "$REPO/tests/$t" "$DIR/tests/"
    TESTS_WIN="$TESTS_WIN '$(wslpath -w "$DIR/tests/$t")'"
done
BASETEMP_WIN="$(wslpath -w "$DIR")\\tmp"   # pytest's tmp_path stays inside DIR

echo "[windows-transport] running in $(wslpath -w "$DIR")"
set +e
powershell.exe -NoProfile -Command \
    "\$env:PYTHONDONTWRITEBYTECODE='1'; $WIN_PY -m pytest -q -p no:cacheprovider -rs --rootdir='$(wslpath -w "$DIR")' --basetemp='$BASETEMP_WIN' $TESTS_WIN $*; exit \$LASTEXITCODE"
rc=$?
set -e
echo "[windows-transport] exit $rc; removing $(wslpath -w "$DIR")"
exit $rc
