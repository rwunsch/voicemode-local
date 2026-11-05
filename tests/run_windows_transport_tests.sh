#!/bin/bash
# Run tests/test_control_transport_windows.py on real Windows, from WSL.
#
# Copies patches/control_transport.py and the test file into a fresh folder
# under the Windows temp dir (same patches/ + tests/ layout as the repo), runs
# pytest there with Windows Python, and deletes the folder on exit. No audio.
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

cp "$REPO/patches/control_transport.py" "$DIR/patches/"
cp "$REPO/tests/test_control_transport_windows.py" "$DIR/tests/"
TEST_WIN="$(wslpath -w "$DIR/tests/test_control_transport_windows.py")"
BASETEMP_WIN="$(wslpath -w "$DIR")\\tmp"   # pytest's tmp_path stays inside DIR

echo "[windows-transport] running in $(wslpath -w "$DIR")"
set +e
powershell.exe -NoProfile -Command \
    "\$env:PYTHONDONTWRITEBYTECODE='1'; $WIN_PY -m pytest -q -p no:cacheprovider -rs --rootdir='$(wslpath -w "$DIR")' --basetemp='$BASETEMP_WIN' '$TEST_WIN' $*; exit \$LASTEXITCODE"
rc=$?
set -e
echo "[windows-transport] exit $rc; removing $(wslpath -w "$DIR")"
exit $rc
