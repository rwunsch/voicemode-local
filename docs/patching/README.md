# Patching voice-mode

How voicemode-local changes the installed `voice_mode` package, in which order, and what
each patch script promises. Written 2026-11-05 with the move from `apply.sh` to `apply.py`.

## How it runs

`patches/manifest.json` is the list. `patches/apply.py` carries it out; `patches/apply.sh`
only picks the venv's Python and calls it, so `install.sh` and other callers are unchanged.

```bash
./patches/apply.sh [venv]                                   # as before
python patches/apply.py [venv]                              # the same, any OS
python patches/apply.py --package-dir <path>/voice_mode     # a package dir directly
```

In order, `apply.py`:

1. checks that every script and module the manifest names exists. A missing one is an
   error. The old shell loop skipped it silently, which is how `install.ps1` came to run
   no patch at all.
2. breaks hard links on the `unshare` files. `uv pip install` hard-links site-packages
   from `~/.cache/uv`, so patching in place would write through to the cache and to every
   venv built from it. Same copy-then-rename as before. A test checks that every patch
   target is on the list.
3. runs the steps in order: `python <script> <voice_mode>/<target>` for each target, or
   copies a module in. It stops at the first failure and passes on that exit code, so a
   copied module never lands without the patch that expects it, and the other way round.

Checked byte-identical: on a scratch copy of pristine 8.12.0 (193 files), the new
`apply.sh` and `apply.py` produced the same content, file modes and log as `apply.sh` at
8055781, and the hard-linked file stayed pristine. `tests/test_apply_py.py` redoes this
on every run, taking the old `patches/` from git.

## Order

| # | Step | Targets | Depends on |
|---|---|---|---|
| 1 | patch_listen_overrun | converse | none |
| 2 | patch_turns_playback_stop | converse | none |
| 3 | patch_simple_failover | simple_failover | none |
| 4 | patch_session_name | converse | none |
| 5 | patch_control_hold | control_channel | none |
| 6 | patch_converse_hold | converse | none |
| 7 | patch_hold_barges_in | core, converse, streaming | 5, 6 |
| 8 | patch_voice_language | converse | none |
| 9 | patch_voice_barge | control_channel, control_socket, core, converse | 5 if present |
| 10 | copy control_transport, ptt_*, voice_barge, voice_pill_state, voice_pill | package | none |
| 11 | patch_voice_pill | control_channel, control_socket, converse, streaming | 9; 4 if present; 10 |
| 12 | patch_control_transport | control_socket | 10 |
| 13 | copy voice_barge_tool | tools/ | none |

Optional dependencies, so FELIX's tree (no hold or session-name patches) works:

- **barge without the hold patches.** If `control_channel.py` has the hold marker, barge
  anchors on the hold patches' text, exactly as before. Without the marker it uses
  pristine 8.12 anchors. The marker decides, so drifted hold text fails instead of
  quietly taking the other path.
- **pill without patch_session_name.** If `_vml_session_name` is defined, the pill uses
  it, as before. If not, the pill adds `_voice_pill_session_name`:
  `VOICEMODE_SESSION_NAME`, else the folder name, else `converse`. It reads no file
  under a fixed home path.

Tested trees (`tests/test_patch_trees.py`), each applied twice:

- pristine plus barge, pill and transport;
- pristine plus FELIX's patches in FELIX's `dependencies.json` order;
- FELIX's patches, then barge, pill and transport;
- the full manifest.

Each must compile, carry its markers, and use no global that the pristine file leaves
undefined. That last check is what caught the pill calling `_vml_session_name` with
patch_session_name absent. FELIX's scripts are copied verbatim into
`tests/fixtures/felix_patches/` from felix-framework aea841ffd.

## What every patch script promises

- It runs as `python <script> <target file>` and needs only the standard library.
- It is idempotent. A marker in the target means "already patched", so it exits 0 and
  writes nothing.
- It reads and writes UTF-8 whatever the locale, so `PYTHONUTF8=1` or not makes no
  difference. `apply.py` sets it for the scripts anyway.
- It accepts CRLF and writes the file back with the line endings it had. Before this,
  `write_text` would have written CRLF on Windows. That is inferred from Python's
  documented behaviour; I did not run the old scripts on Windows.
- On drift (an anchor not found exactly as often as expected) it prints `ANCHOR DRIFT`
  to stderr, exits non-zero and leaves the file untouched.

FELIX's three scripts read CRLF but always write LF. That is a strict xfail in the tests:
it starts failing as soon as FELIX keeps line endings.

## A venv patched earlier keeps the old patch text

The marker makes a patch idempotent, but it also means a patch whose text changed is
**not** re-applied to a file that already carries its marker. Changing a patch therefore
needs pristine files again:

```bash
uv pip install --python .venv/bin/python3 --reinstall-package voice-mode "voice-mode==8.12.0"
./patches/apply.sh
```

You can also delete the venv and run `install.sh` again. Plain `install.sh` on an
existing venv does not reinstall voice-mode, so the old text stays. FELIX rebuilds its
runtime whenever a listed input changes, so it picks up new text by itself.

## Tests

- `tests/test_patch_trees.py`: the four trees, CRLF, and a drift case per patch.
- `tests/test_apply_py.py`: the byte-identical check against the old `apply.sh`, the
  hard-link break, and how the manifest is handled.
- `tests/test_apply_windows.py`, through `VM812_SRC=... tests/run_windows_transport_tests.sh`:
  `apply.py` under `py -3.14` on pristine copies in a temp folder (deleted afterwards).
  The output must match what Linux produced, byte for byte. LF and CRLF input are both
  tested, with and without `PYTHONUTF8`.

`VM812_SRC` must also hold `server.py`, which FELIX's patches modify.
