# Native Windows runtime, speech services and launcher

> **TL;DR:** `install.ps1 -Mode windows-native` sets it up and registers the MCP server.
> `scripts/windows/voicemode_native.py` installs a private runtime under
> `%LOCALAPPDATA%\voicemode-local-native`, starts loopback-only speech services on private
> ports, and is the MCP command on native Windows (no WSL, no Docker). Tested on
> Windows from install to stop, with no audio. A real spoken conversation is
> still unproven.

| Field        | Value |
|--------------|-------|
| Status       | In progress (tasks 4, 6 and 8 of the native Windows plan done) |
| Created      | 2026-11-05 |
| Last Updated | 2026-11-06 |
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
  folders, and outside the profile and `%LOCALAPPDATA%` unless `--allow-root-outside-profile`
  is given, because the runtime runs code from that folder. The default root is not the
  WSL pill's `%LOCALAPPDATA%\voicemode-local`. uv's Python (`UV_PYTHON_INSTALL_DIR`) and its
  cache live under the root, and the cache is removed after setup, whether it succeeded or
  failed. Before selecting a runtime, setup has Kokoro synthesise one short text into memory;
  no sound is output. After selecting it, setup removes every runtime that is neither active
  nor named by a live lease or a running service. Every download is checked against the URL, size and SHA-256 pinned in
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
  TTS and STT services, then the pill (`VOICEMODE_PILL=true`) and the voice barge-in trigger
  (`VOICEMODE_VOICE_BARGE=true`; `python -m voice_mode.voice_barge <silero>`, hearing the mic through
  sounddevice). Each is one per install, under the same lease and watchdog. Push-to-talk is refused natively, with
  one line of explanation on stderr, until task 7: `ptt-listener-windows.ps1` still sends to
  the WSL relay on 127.0.0.1:8765, so it would drive the WSL install. It waits until TTS and STT answer and checks that the process
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
- **A crashed service.** The watchdog checks every 3 s. If the TTS or STT service has died, it
  starts it again with the recorded command on the recorded port, but only while that port is
  free, and at most 3 times in 5 minutes. Each restart is written to `run\watch.log`. Past that limit, or when another program has
  taken the port, nothing restarts the service until the next connector starts. A connector
  that is already running keeps the URL it was given. The pill and the listener are never
  restarted.
- **The pill window, native (task 6).** `optional_command("pill")` starts `pill-windows.ps1`
  with `-BackendExe <runtime python> -BackendArgs "-m voice_mode.voice_pill --stdio"
  -DataDir <root>\pill`. The window runs the backend directly, without WSL, keeps its files
  in `<root>\pill`, and uses a mutex and stop event named after a hash of that folder: one
  pill per install. `pill_stop_event(root)` returns the stop event's name, for the native
  switch (task 8) to set. `-BackendArgs` is one Windows command line (`subprocess.list2cmdline`):
  PowerShell's `-File` binds a value that starts with `-` correctly (checked 2026-11-06,
  n=3 strings, one with quotes and a non-ASCII letter). Details in `docs/voice-pill/README.md`,
  section "Windows native".
- **Tests.** `tests/test_voicemode_native.py` has the launcher's and the switch's Linux tests, `tests/test_install_ps1.py` install.ps1's, and `tests/test_conftest_release_check.py` 3 more. `tests/test_voicemode_native_windows.py`
  has 8 Windows tests (4 added for the switch and install.ps1), run by `tests/run_windows_transport_tests.sh`. `tests/conftest.py`
  turns a skip caused by a missing `VM812_SRC` into a failure under `VOICEMODE_RELEASE_CHECK=1`,
  and prints a loud warning otherwise.

Not done here: the native mode of `ptt-listener-windows.ps1` (task 7; it still sends to the
WSL relay). The launcher starts the pill only when enabled, and push-to-talk not at all yet.
The pill's native tests are `tests/test_pill_windows_native.py` (Linux: static checks, and
the name derivation compared with PowerShell's through `powershell.exe`) and
`tests/test_pill_windows_native_windows.py` (Windows, 9 tests). MCP registration and `install.ps1` belong to
task 8.

## Install, switch, uninstall (task 8)

**Install.** From a Windows folder, not a `\\wsl` path:

```powershell
powershell -ExecutionPolicy Bypass -File C:\git\voicemode-local-native\install.ps1 -Mode windows-native
```

It runs in Windows PowerShell 5.1 and in PowerShell 7, needs no admin rights and no Docker,
and never touches WSL. It finds a Python 3.10 or newer (`py -3`, then `python`) and uv
(`VOICEMODE_UV`, then PATH, then `%USERPROFILE%\.local\bin`). If uv is missing it prints
the command that installs it for your user and stops; `-InstallUv` runs that command. Then it
runs `voicemode_native.py setup` and registers the MCP server:
`claude mcp add --scope user voicemode -- <python> <script> launch` when the claude CLI is
found, else it writes `mcpServers.voicemode` into `~\.claude.json`, keeping everything else
in the file and leaving a backup beside it. With `-Root`, the entry also carries
`VOICEMODE_NATIVE_ROOT`. An existing `voicemode` entry that differs is printed and left
alone; `-Force` replaces it. `-Pill` and `-Barge` switch those on after setup; both are off
by default. The summary prints the root, the runtime, the disk use from `status`, the MCP
entry, and the switch and uninstall commands.

`<python>` in the MCP entry is uv's minor-version link to its own Python under the root,
`<root>\python\cpython-3.12-windows-x86_64-none\python.exe`. uv moves that link to each new
patch release, so the path outlives runtime rebuilds. A runtime's own venv Python does not:
each rebuild gets a new folder. That Python has no psutil, so `main()` restarts itself under
the runtime's Python, the path the tests have always taken. If the link is missing, the
Python install.ps1 found is used, and the summary says so.

A `\\wsl` checkout is refused unless `-AllowWslCheckout` is given: the MCP command would run
the launcher from there, so voice would work only while WSL runs.

install.ps1 now starts with a UTF-8 BOM. Without one, Windows PowerShell 5.1 reads the file
in the machine's ANSI code page; the file read as cp1252 fails to parse with 7 errors,
because the bytes of an em dash include a curly quote that ends a string (measured
2026-11-06 by decoding it as cp1252 and parsing it; this machine's ANSI code page is 65001,
so it cannot show the failure directly). The two WSL modes still need PowerShell 7 and now
say so themselves, instead of `#requires`. Their code is byte for byte what it was, and a
test checks that.

**Switch.** No restart:

```powershell
& <python> <repo>\scripts\windows\voicemode_native.py barge on|off|status
& <python> <repo>\scripts\windows\voicemode_native.py pill on|off|status
```

`on` and `off` write `VOICEMODE_VOICE_BARGE` or `VOICEMODE_PILL` to
`%USERPROFILE%\.voicemode\voicemode.env`. Other lines, comments, a BOM and the line endings
stay; the file is replaced atomically and created if missing. While a voice connector runs,
`on` starts the trigger or the pill window into the install's services Job Object, one per
install, recorded in `run\state.json` under the service lock, as `ensure_services` does.
`off` closes the pill through its stop event, then kills what is left after 5 s; the trigger
is killed. Without a running connector, the setting takes effect with the next one.
`barge on` refuses, and exits 1, when the Silero model is missing from the runtime. Like
`voicemode-switch barge on`, it adds `voice_barge_tool` to a `VOICEMODE_TOOLS_ENABLED` list
that already exists, and never creates one.

`status` prints one line, then whether the process runs. For barge the first line is exactly
what `python -m voice_mode.voice_barge status` prints: `on`, `off` or `cannot run: <reason>`.
For the pill it is `on` or `off`. The second line is `trigger: running (process N)`,
`window: not running` and so on. Both read voicemode.env first, as the switch writes it.

The environment wins over voicemode.env when the launcher decides what to start. If the
environment holds a different value, the switch says so. Under a connector only what the
launcher's own environment held counts (`VOICEMODE_NATIVE_ENV_SETTINGS`): voice-mode copies
voicemode.env's old values into its environment when it starts, and those are no override.

The launcher passes `VOICEMODE_BARGE_CONTROL_COMMAND=["<runtime python>", "<script>", "barge"]`
to voice-mode and to the pill backend, so the `voice_barge` tool and the pill's toggle run
the native switch. It also passes `VOICEMODE_BARGE_PYTHON` (the runtime's Python) and
`VOICEMODE_NATIVE_ROOT`. A process voice-mode starts sits in the connector's Job Object; the
connector's job now allows breakaway on request, and `spawn` asks for it, so what the switch
starts lands in the services' job and outlives that connector.

voice-mode 8.12 loads only `converse` and `service` unless a tool setting says otherwise, so
the `voice_barge` tool did not exist natively. The launcher now passes
`VOICEMODE_TOOLS_ENABLED=converse,service,voice_barge_tool` when no tool setting is in the
environment or voicemode.env. A list you set yourself is left alone.

**Control channel and streaming.** When the pill, barge or push-to-talk is on (environment
first, then voicemode.env), the launcher passes `VOICEMODE_CONTROL_CHANNEL_ENABLED=true` to
voice-mode. Without it the trigger never sees that the assistant is speaking and never fires:
the controller's spoken test on 2026-11-06 got 0 cuts without it and 2 of 2 with it set by
hand. Natively the channel is the named pipe from `patch_control_transport.py`; a test fails
if the native manifest loses that patch. With barge on, the launcher also passes
`VOICEMODE_STREAMING_ENABLED=false`, because the trigger is inert while playback streams
(`docs/voice-barge-in/README.md`). Either value you set yourself, in the environment or in
voicemode.env, is kept. The switch writes voicemode.env, so the next connector picks these up.
A connector that was already running without the channel cannot be interrupted until it is
reconnected (`/mcp`); `barge on` and `pill on` say so.

**Measured on Windows, 2026-11-06** (`tests/test_voicemode_native_windows.py`, one run, temp
root and profile): `barge on` then `off` with a `serve` running, 2 cycles: the trigger started,
was in the services Job Object (`IsProcessInJob`), was alive 4 s later, and was gone after
`off`; `status` matched `python -m voice_mode.voice_barge status`. `pill on` then `off`, 2
cycles: the window and its backend started, the window stayed hidden (no voice session, as
designed), and `off` closed it through the stop event (`stop requested` in its log), with
nothing left. The barge command read from the pill backend's own environment ran and answered
(n=2). The `voice_barge` tool, called over MCP stdio from a `launch`, started the trigger into
the services' job and stopped it (n=1). install.ps1 under Windows PowerShell 5.1.26100 with a
fake claude CLI and a temp profile: install with `-Pill -Barge`, the registered command
answered `initialize` and started tts, stt, barge and pill; a second run kept the entry; a
different entry was kept without `-Force` and replaced with it; `-Uninstall`, then
`-Uninstall -RemoveData` (n=1 each). `status` reported 1,078,627,505 bytes for the root, with
uv's Python at 62,939,825 bytes counted once. The 125,879,649 in the table below was summed
with `rglob`, which also walks uv's `cpython-3.12-windows-x86_64-none` link to the same
folder, so it likely counts that Python twice (not re-measured otherwise).

Not tested here: a clean machine. With no Python, install.ps1 stops and names python.org;
with no uv, it prints the installer command (`-InstallUv` runs it, which was not run here);
with no WebView2 runtime, the pill window cannot show. Each of these, and a machine whose
ANSI code page is not UTF-8, still needs a real run.

**Uninstall.** `install.ps1 -Mode windows-native -Uninstall` stops the services and removes the
MCP entry only if it is this install's: this checkout's script and this root (an entry without
`VOICEMODE_NATIVE_ROOT` belongs to the default root). Another install's entry, even one from the
same checkout, is printed and kept. If `claude mcp remove` fails, the uninstall says so and stops.
`-RemoveData` then runs `voicemode_native.py remove-data`, which deletes the root. It refuses the
WSL pill's `%LOCALAPPDATA%\voicemode-local` and FELIX's folders, anything inside them, the
profile and `%LOCALAPPDATA%` themselves, any folder that holds one of these, and a folder
without `active.json` or `runtime`. voicemode.env keeps its lines. `native_root()` also refuses a
root that is the profile, `%LOCALAPPDATA%`, or a folder holding either.

**Two smaller guarantees.** The switch changes voicemode.env under a lock file beside it
(`voicemode.env.lock`, msvcrt or fcntl), so two switches never lose each other's change; other
writers of that file (voicemode-switch, upstream's `update_config`) do not take it. And a
process that cannot be put in its Job Object is killed before its first instruction and the
start fails with a next step, instead of running unsupervised. A setting counts as on when it
says `true` in any case, with or without quotes, as `barge_state` reads it.

## What is installed, measured

Measured 2026-11-05 on this machine (Windows 11, x64), n=4 installs; sizes from the last, by
`tests/test_voicemode_native_windows.py` and a separate `pip download`.

| Item | Download (bytes) | On disk (bytes) | How measured |
|---|---|---|---|
| Python packages: 115 wheels, cp312 win_amd64 | 131,574,458 | `.venv` 472,963,284 (20,558 files) | `pip download --require-hashes --only-binary :all: --platform win_amd64 --python-version 3.12` of the lock, filtered by its markers for that target, into a temp folder (deleted); `.venv` summed after setup |
| `kokoro-v1.0.onnx` | 325,532,387 | same, in `downloads\` | size and SHA-256 checked by setup |
| `voices-v1.0.bin` | 28,214,398 | same | as above |
| `ggml-base.bin` (Whisper base) | 147,951,465 | same | as above |
| `silero_vad.onnx` (barge-in, added 2026-11-06) | 2,327,524 | same | as above; size and SHA-256 also checked against the WSL copy. Not in the totals below, which predate it |
| `whisper-bin-x64.zip` (whisper.cpp b5454) | 8,928,640 | zip kept in `downloads\`; unpacked `services\whisper` 17,687,552 | as above; folder summed |
| Model links in the runtime | 0 | 0 extra | hard links, `st_nlink >= 2` asserted |
| Python 3.12, uv-managed, in `<root>\python` | not measured | 125,879,649 | folder summed |
| uv cache | | 0 after setup | removed; asserted absent |
| **Total** | **642,201,348** plus Python | **1,064,229,839** in 23,919 files | the whole root after one setup, each hard-linked file counted once |

The `downloads\` folder summed to 510,626,890 bytes, exactly the four pinned sizes. Not
measured: the size of Python 3.12's download. uv fetches it into the root at setup. In the test the four pinned files were copied, read-only, from FELIX's models
folder (`VML_MODEL_SOURCE`) instead of being downloaded, and setup verified every one.
Setup took 111 s, 92 s, 99 s and 105 s with the models seeded. voice-mode answered `initialize`
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

- **voice-mode runs in the runtime folder, not the client's.** Upstream loads every
  `.voicemode/voicemode.env` it finds walking up from its working folder. Started from a WSL
  checkout (`\\wsl.localhost\Ubuntu\home\...`), native voice-mode read the WSL setup's file on
  2026-11-06: streaming off and debug on, so TTS took the buffered path and audio was saved to
  `C:\Users\<you>\.voicemode\audio`. The launcher now passes `cwd=<runtime>` and hands the
  client's folder to the session label as `VOICEMODE_CLIENT_FOLDER`
  (`patch_native_client_folder.py`). Cost: per-project `voicemode.env` files are not read
  natively; `~\.voicemode\voicemode.env` and the environment still are.
- **Windows cannot delete an open file.** voice-mode deleted each TTS temp file inside the
  `with` that held it open: WinError 32 after playback, so every buffered turn failed and
  never listened. `patch_windows_tempfile.py` closes first. Upstream 9.0.2 still has it.
- **Check the default input before a spoken test.** On Robert's laptop the default input
  (Jabra Engage 75) recorded digital silence (rms 0.5 over 120 s, n=2) while other mics had
  signal; voice-mode then reports "No speech detected" after the full listen.

- **No ffmpeg natively.** voice-mode 8.12 refuses `converse` without ffmpeg. Native setup
  therefore applies `patches/manifest.windows.json` (`apply.py --native-windows`): our list
  plus FELIX's `patch_no_ffmpeg_gate.py`, `patch_no_ffmpeg_warning.py` and
  `patch_load_scipy_early.py`, in FELIX's place right after `patch_simple_failover.py`. They are copied from
  aea841ffd and changed only to keep CRLF. WSL and Mac keep ffmpeg and `manifest.json`;
  `tests/test_native_manifest.py` checks that their output is byte-identical to d30ec68.
  The Windows test strips every ffmpeg/ffprobe folder from PATH.
- **The pill reads `voicemode.env` from the home folder, not the base dir.** The task brief
  said `VOICEMODE_BASE_DIR`; `voice_pill_state.setting`, which the launcher uses to decide
  whether to start the pill, reads `~\.voicemode\voicemode.env` whatever the base dir. The
  window follows the resolver, so the two cannot disagree.
- **Any spelling of one folder gives one pill name.** Both sides hash the full long path:
  `GetFullPath` (absolute, `/` as `\`, `.` and `..` resolved, trailing dots and spaces of the
  last part dropped), then 8.3 short names expanded through `GetLongPathNameW` for the longest
  prefix that exists. `GetFullPath` in PowerShell 5.1 expands only some short names, depending
  on what exists (measured 2026-11-06, 5 paths), so the script expands them itself, as
  `windows_full_path` does in Python. Then trailing slashes are dropped and the result is
  lowercased. Python's `str.lower()` and .NET's `ToLowerInvariant()` agree for the paths tested
  (ASCII, spaces, `ü`); they may differ for rare letters such as Turkish `İ`.
- **Each install downloads its own WebView2 DLLs** into `<root>\pill\webview2`, once: two
  files, 898,240 bytes (measured 2026-11-06 in the WSL pill's copy of version 1.0.4314.35). The
  spec gives 906,432 kept and 9,818,548 downloaded; not re-measured here.
- The root path must stay short: the espeak data path has a 160-byte limit, and Windows a
  260-character one. Setup checks both.
- The settings resolver reads `~/.voicemode/voicemode.env`, the user's own. The tests point
  HOME and USERPROFILE at a temp folder.
