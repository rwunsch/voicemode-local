# Native Windows runtime, speech services and launcher

> **TL;DR:** `scripts/windows/voicemode_native.py` installs a private runtime under
> `%LOCALAPPDATA%\voicemode-local`, starts loopback-only speech services on private
> ports, and is the MCP command on native Windows (no WSL, no Docker). Tested on
> Windows from install to stop, with no audio. A real spoken conversation is
> still unproven.

| Field        | Value |
|--------------|-------|
| Status       | In progress (task 4 of the native Windows plan) |
| Created      | 2026-11-05 |
| Last Updated | 2026-11-05 |
| Project      | voicemode-local, branch feature/windows-native |

## Why

Spec `docs/superpowers/specs/2026-11-05-windows-native-design.md`, sections 3 and 5.
A `.cmd` cannot be an MCP command (`docs/windows-issues.md`, issue 4), the old
`windows-native` install mode needed Docker, and both speech servers bound 0.0.0.0.

## What was done

Adapted from FELIX's `plugins/voicemode/scripts/voice.py` and `launch.js` at
felix-framework commit aea841ffd. The code was copied, not imported, and the header comment records
where it came from.

- **Install** (`setup`). Into `<root>\runtime\<digest12>-<random>`: `uv venv --managed-python`
  for Python 3.12, then `uv pip sync --require-hashes --only-binary :all: --link-mode copy
  --compile-bytecode` from `scripts/windows/requirements.lock` (FELIX's lock, unchanged).
  `patches/apply.py --native-windows` patches it. whisper-server comes from the pinned zip, and the models are
  hard links to `<root>\downloads\<sha256>\`. `active.json` is written last, atomically, so a
  half-built runtime is never selected. The folder name has a random suffix, as in FELIX, so a
  rebuild never touches a runtime that is still in use. Setup refuses a root inside FELIX's
  folders. Every download is checked against the URL, size and SHA-256 pinned in
  `scripts/windows/dependencies.json`.
- **Services.** Kokoro (`kokoro-onnx-server.py --host 127.0.0.1`) and whisper-server
  (`--host 127.0.0.1`, no `--convert`, so no ffmpeg), each on the first free port of
  28880-28899 and 22022-22041. Free means nothing answers and loopback binds. These
  ports are never used: 8880-8882, 2022, 9000, and FELIX's 18880-18899 and 12022-12041.
  `VOICEMODE_NATIVE_TTS_PORT` and `_STT_PORT` set a port to try first.
- **`--host`.** `kokoro-onnx-server.py` gained `--host`, default `0.0.0.0` as before.
  `whisper-proxy.py` already had one (commit 951d46e), and a test now pins its default.
  Natively, STT is whisper-server itself, so the proxy is not used.
- **Launcher** (`launch`, the MCP command). It writes the lease, starts the watchdog, then the
  TTS and STT services, then the pill (`VOICEMODE_PILL=true`) and the push-to-talk listener
  (`VOICEMODE_PTT_ENABLED=true`). It waits until TTS and STT answer and checks that the process
  answering is the one it started. Then it runs voice-mode with stdin, stdout and stderr passed
  by name (FELIX's `connector_options`). Settings come from the environment first, then
  `voicemode.env`, through `patches/voice_pill_state.setting`.
- **Lifetime.** Each connector holds a lease. The services, pill and listener sit in one named
  Job Object per install (`Local\voicemode-local-services-<hash of root>`), with
  kill-on-close. Every connector and the watchdog hold a handle to it. voice-mode sits in its
  connector's own Job Object. When `main()` restarts itself under the runtime's Python (the
  starting Python lacks psutil), the outer job admits only the inner launcher (silent
  breakaway), because a process already in a job can join only a job nested inside it. A
  process is created suspended, put in its job, then resumed, so it cannot start a child
  outside the job first. Process trees are stopped with `kill_tree` (psutil). Nothing calls
  `os.kill(pid, 0)`; a test checks the source for it.
- **Tests.** `tests/test_voicemode_native.py` has 37 Linux tests, and `tests/test_conftest_release_check.py` 3 more. `tests/test_voicemode_native_windows.py`
  has 4 Windows tests, run by `tests/run_windows_transport_tests.sh`. `tests/conftest.py`
  turns a skip caused by a missing `VM812_SRC` into a failure under `VOICEMODE_RELEASE_CHECK=1`,
  and prints a loud warning otherwise.

Not done here: native modes of `pill-windows.ps1` (task 6; it still throws without
`-Distro`) and `ptt-listener-windows.ps1` (task 7; it still sends to the WSL relay).
The launcher starts both only when enabled. MCP registration and `install.ps1` belong to
task 8.

## What is installed, measured

Measured 2026-11-05 on this machine (Windows 11, x64), n=3 installs, by
`tests/test_voicemode_native_windows.py` and a separate `pip download`.

| Item | Download (bytes) | On disk (bytes) | How measured |
|---|---|---|---|
| Python packages: 115 wheels, cp312 win_amd64 | 131,574,458 | `.venv` 473,371,898 (20,558 files; native patch list) | `pip download --require-hashes --only-binary :all: --platform win_amd64 --python-version 3.12` of the lock, filtered by its markers for that target, into a temp folder (deleted); `.venv` summed after setup |
| `kokoro-v1.0.onnx` | 325,532,387 | same, in `downloads\` | size and SHA-256 checked by setup |
| `voices-v1.0.bin` | 28,214,398 | same | as above |
| `ggml-base.bin` (Whisper base) | 147,951,465 | same | as above |
| `whisper-bin-x64.zip` (whisper.cpp b5454) | 8,928,640 | zip kept in `downloads\`; unpacked `services\whisper` 17,687,552 | as above; folder summed |
| Model links in the runtime | 0 | 0 extra | hard links, `st_nlink >= 2` asserted |
| uv cache | | 0 after setup | removed; asserted absent |
| **Total** | **642,201,348** | **1,001,686,340** plus two small files (`kokoro-onnx-server.py`, `dependencies.json`) | sums of the rows |

The `downloads\` folder summed to 510,626,890 bytes, exactly the four pinned sizes. Not
measured: Python 3.12 itself. uv downloads it when it has no managed 3.12, and this machine
already had one. In the test the four pinned files were copied, read-only, from FELIX's models
folder (`VML_MODEL_SOURCE`) instead of being downloaded, and setup verified every one.
Setup took 111 s, 92 s and 99 s with the models seeded. voice-mode answered `initialize`
in 10.7-13.0 s (n=4), and 14.0-15.0 s with ffmpeg off PATH (n=2).

## How to recreate

```powershell
# any Python 3.10+ and uv on PATH; no admin rights
py -3.14 <repo>\scripts\windows\voicemode_native.py setup
py -3.14 <repo>\scripts\windows\voicemode_native.py status
# MCP command: <python> <repo>\scripts\windows\voicemode_native.py launch
```

Tests, from WSL:

```bash
VML_MODEL_SOURCE='C:\path\to\<sha256>\<file> folder' VM812_SRC=<pristine 8.12.0> \
  VOICEMODE_RELEASE_CHECK=1 tests/run_windows_transport_tests.sh -s
```

## Gotchas

- **No ffmpeg natively.** voice-mode 8.12 refuses `converse` without ffmpeg. Native setup
  therefore applies `patches/manifest.windows.json` (`apply.py --native-windows`): our list
  plus FELIX's `patch_no_ffmpeg_gate.py`, `patch_no_ffmpeg_warning.py` and
  `patch_load_scipy_early.py`, in FELIX's place right after `patch_simple_failover.py`. They are copied from
  aea841ffd and changed only to keep CRLF. WSL and Mac keep ffmpeg and `manifest.json`;
  `tests/test_native_manifest.py` checks that their output is byte-identical to d30ec68.
  The Windows test strips every ffmpeg/ffprobe folder from PATH.
- The root path must stay short: the espeak data path has a 160-byte limit, and Windows a
  260-character one. Setup checks both.
- The settings resolver reads `~/.voicemode/voicemode.env`, the user's own. The tests point
  HOME and USERPROFILE at a temp folder.
