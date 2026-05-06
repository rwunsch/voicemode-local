# Windows porting issues in `voice-mode` 8.6.1

These were uncovered while wiring `voice-mode` into Claude Code on Windows
(Claude Desktop CLI, Windows 11). Workarounds are in `patches/` and `install.ps1`.

The current dual-platform setup sidesteps issues 4-6 by having the Windows
Claude Code MCP entry invoke `voice-mode` inside WSL via `wsl.exe` — proxies
and Docker containers are shared, audio is routed through WSLg.

## 1. POSIX-only `fcntl` import in `conch.py`  *(workaround applied)*

`voice_mode/conch.py:27`: `import fcntl` at module top. POSIX-only, so
importing `voice_mode.tools.converse` (which imports `conch`) explodes on
Windows with `ModuleNotFoundError: No module named 'fcntl'`.

**Workaround:** `patches/conch.py` provides an `msvcrt`-backed shim when
`sys.platform == "win32"`. Linux behavior is unchanged (the real `fcntl` is
still imported on non-win32).

**Upstream fix candidate:** make conch's locking pluggable, or use a
cross-platform lock library (`portalocker`, `filelock`).

## 2. POSIX-only `resource` import in `tools/converse.py`  *(workaround applied)*

`voice_mode/tools/converse.py` lines 1377 and 2110: `import resource` for
`resource.getrusage` memory metrics. Only triggered when
`VOICEMODE_DEBUG=true`, but turning on debug to investigate other issues
crashed the tool.

**Workaround:** `patches/converse_tool.py` wraps both imports in
`try/except ImportError`. Memory metric is just skipped on Windows.

**Upstream fix candidate:** check `sys.platform != "win32"` before importing,
or use `psutil` (already a transitive dep through openai).

## 3. ffmpeg detection via `shutil.which('ffmpeg')` is fragile under Claude Code  *(workaround applied)*

`voice_mode/utils/ffmpeg_check.py` uses `shutil.which('ffmpeg')`. Returns
`None` if PATH doesn't include the directory containing `ffmpeg.exe`. With
ffmpeg installed via winget (default location
`%LOCALAPPDATA%\Microsoft\WinGet\Links`), any process launched from a shell
that started before the winget install does NOT have that directory on PATH —
so the MCP server inherits a PATH without ffmpeg even though the user
"installed it".

**Workaround:** Windows install script puts an explicit `PATH` value into the
MCP `env` block in `~/.claude.json` that prepends the winget Links dir. The
spawned MCP server then sees ffmpeg regardless of parent shell vintage.

A `voice-mode-launcher.cmd` wrapper was tried first — see issue 4.

**Upstream fix candidate:** when `shutil.which('ffmpeg')` fails on Windows,
also probe `%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe` and similar
common locations (Chocolatey's `C:\ProgramData\chocolatey\bin`, Scoop's
`%USERPROFILE%\scoop\shims`).

## 4. Claude Code on Windows does NOT honor `.cmd` files as MCP `command`  *(workaround applied)*

We initially solved issue 3 with a `.cmd` wrapper that prepended PATH then
ran `voice-mode.exe`. The wrapper logic was correct (verified by running it
manually), but Claude Code's MCP loader on Windows never invoked it: the
launcher's debug log was never written, yet `voice-mode.exe` still appeared
as a child of `claude.exe`. Probable cause: Node-side `child_process.spawn`
without `{shell: true}` doesn't execute `.cmd` files; some fallback got us
running `voice-mode.exe` directly with the wrong (parent-inherited) env.

**Workaround:** abandon the `.cmd` wrapper, register `voice-mode.exe`
directly, set PATH inside the MCP `env` block (issue 3).

**This is a Claude Code issue, not voice-mode.** Worth filing upstream against
Claude Code: either spawn with shell on Windows, or document the
limitation. For now the MCP `command` field on Windows must be an `.exe`.

## 5. Recording loop hangs when audio callbacks starve  *(NOT workaround-able cleanly)*

`voice_mode/tools/converse.py:1000-1078` — the `record_audio_with_silence_detection`
loop derives `recording_duration` solely from chunks pulled out of the queue
(line 1074). If sounddevice's callback doesn't deliver into the queue,
`recording_duration` stays at 0 forever and `max_duration` is never reached.
The loop's only other exit is VAD silence detection, which can't fire if no
chunks arrive.

On Windows + Jabra Engage 75 (USB headset, MME backend), this manifested as:
- Stream opens cleanly
- "Started continuous audio stream" logged
- Then absolute silence in the log for 4+ minutes until the user pressed ESC
- Standalone `sd.InputStream(...)` probes at the same params (24 kHz / mono /
  int16 / 30 ms blocks) DO deliver 33 callbacks/sec correctly

So callbacks reach the queue in isolation but evidently not in voice-mode's
threading context. Root cause un-identified. Could be WASAPI vs MME, executor
thread vs main thread, GIL contention, or audio device exclusivity.

**Two upstream fixes worth proposing:**
1. `recording_duration` should be derived from `time.monotonic()` deltas, not
   from received chunk counts. This caps the worst case at `max_duration`
   regardless of callback delivery.
2. The `queue.Empty` branch (line 1077-1078) should at least log periodically
   so this kind of starvation is visible without VAD_DEBUG.

**Current workaround:** none in voice-mode itself. The Windows MCP entry now
invokes voice-mode inside WSL via `wsl.exe`, where the audio path works
through WSLg.

## 6. TTS temp-file save fails on Windows with `WinError 32`  *(cosmetic)*

`voice_mode/tools/converse.py` saves each TTS clip to a temp file, then tries
to copy/process it while sounddevice or pydub still has it open:

```
ERROR - Failed to save TTS audio: [WinError 32] The process cannot access
the file because it is being used by another process:
'C:\\Users\\wunsch\\AppData\\Local\\Temp\\tmpna_7hd5p.wav'
```

TTS playback still works — only the saved-audio side feature breaks. POSIX
allows reading and unlinking files held open by another process; Windows
does not.

**Upstream fix candidate:** explicitly close the playback handle before the
save, or open the temp file with `delete=False` and clean up after both
playback and save complete.

## Severity summary for upstream

| # | Severity | Frequency | Suggested upstream fix |
|---|----------|-----------|----------------------|
| 1 | Hard fail at import | Always (any tool that uses conch) | `if sys.platform == 'win32'` shim or `portalocker` |
| 2 | Hard fail at runtime | Only when DEBUG=true | `try/except ImportError` |
| 3 | Hard fail at first use | Often (PATH inheritance) | Probe winget/chocolatey/scoop fallback locations |
| 4 | N/A (Claude Code) | n/a | File against Claude Code, not voice-mode |
| 5 | Soft fail (infinite hang) | Common on Windows audio | wall-clock time check + queue-empty logging |
| 6 | Cosmetic (saved audio missing) | Every TTS call | close handle before save |
