# Stall diagnose: a snapshot taken during a voice stall

**Date:** 2026-11-18
**Status:** built (`voicemode-switch diagnose`, `patches/stall_diagnose.py`); never yet run during a real stall

## Why

Voice sometimes stalls for 20-30 s or stutters, mostly when the load is 20-60 on 14 cores, even
though speech is generated on the GPU. Reconnecting the session's MCP service (`/mcp`) helps, and
then whatever was wrong is gone. Nobody knows yet why the reconnect helps. This command records the
state before the reconnect wipes it.

## How to run it during a stall

From any shell, while the voice is stuck and **before** `/mcp`:

```bash
voicemode-switch diagnose
```

It takes a few seconds: 1 s of CPU sampling, under WSL up to 20 s for the Windows host, then up
to 15 s per engine probe, up to 20 s for the conch status and up to 10 s for py-spy dumps, which run in parallel. The report is printed and
written to `~/.voicemode/logs/diagnose-<YYYYmmdd-HHMMSS>.txt`. The last line gives the path. Run
it again after the reconnect, then compare the two files.

From Python, for the pill later: `stall_diagnose.diagnose(stall_diagnose.Env.default())` returns
`(text, suspects, path)`.

For more detail, install py-spy once (`pip install py-spy`). The report then also shows what every
thread of your own voice-mode processes is doing. The command never installs it for you.

## What it does and does not touch

- Read only: `ps`, `/proc`, `pactl`, `ss`, `voicemode conch status`, `py-spy dump --nonblocking`,
  the log files, and under WSL one `powershell.exe -NoProfile -Command` call that only reads
  (`Get-CimInstance`, `Get-Process`, `Get-Service`).
- The only requests it sends: per TTS engine that answers `/health`, one TTS request for the
  sentence "Hello."; per STT engine that answers, one request transcribing 1 s of silence that the
  report makes itself (16 kHz mono WAV).
- It starts, stops and signals nothing. The only file it writes is the report itself.
- It never copies spoken text. Exchange rows show timings only, and debug-log lines that carry
  what was said (`TTS full text:`, `Transcribed:`, `Response text:`, ...) are skipped.
- Each step has its own timeout. A step that fails becomes a `failed: ...` line, and the report
  is still written.
- Engine ports default to 2022, 2023, 8880, 8881 and 8882 on 127.0.0.1. You can override them
  with `VOICEMODE_DIAGNOSE_PORTS="Name:port:tts|stt,..."`; the tests use this to point at a spare
  port.

## Sections

| Section | What it shows | How to read it |
|---|---|---|
| Load | 1/5/15-min load average and the number of cores | Load above 2 x cores means processes are waiting for a CPU. |
| Top CPU | Linux: the 8 busiest processes over a 1 s sample from `/proc` (current, not lifetime), with thread counts. macOS: `ps` %cpu. Windows: not collected. | Shows who is using the CPU at that moment. |
| Windows host | WSL only (`WSL_DISTRO_NAME` set or `/proc/version` mentions microsoft); elsewhere `not collected on this OS (not WSL)`. One `powershell.exe` call (20 s timeout): how long the call took, host CPU load (`Win32_Processor` LoadPercentage, average), logical/physical processors, committed memory %, the `vmmem`/`vmmemWSL` working set, `Audiosrv` and `AudioEndpointBuilder` state, and the 8 busiest Windows processes over 1 s (two `Get-Process` CPU readings), as % of the whole host. Process names and ids only: no command lines, paths or window titles. A timeout or failure is a line. | WSL's own load does not show the host. 2026-11-18: a stall coincided with PulseAudio not answering for 5 s while the host CPU was at 100 % (wsl, chrome, teams, Taskmgr). The PowerShell call itself is slow when the host is overloaded. The PowerShell process shows up in its own top list. |
| voice-mode MCP processes | One line per voice-mode MCP server (pid, ppid, uid, age, threads, cpu), the Claude session that owns it (from `~/.claude/sessions/<claude pid>.json`: id, name, cwd), and on Linux `/proc` State, voluntary/nonvoluntary context switches and how many threads are in each state (R/S/D) | `D` means a thread is stuck in an uninterruptible wait, usually I/O. Many nonvoluntary switches mean the process kept losing the CPU. |
| py-spy | Python stacks of your own voice-mode processes (10 s timeout each), or `py-spy not installed: pip install py-spy` | Shows where a stuck turn is waiting: in PortAudio, in an HTTP request, on the conch. |
| Audio path | Linux with pactl: server, default sink and source, every sink and source with its state (SUSPENDED/IDLE/RUNNING), and the open sink-inputs and source-outputs. macOS and Windows: `not collected on this OS` | A stream on a SUSPENDED device should not happen. |
| Conch | `voicemode conch status`: who holds the floor and who waits | A holder that is not talking points to a wedged floor. |
| Engines | For each engine: health status and identity (engine, provider, model, device), the listening pid from `ss`, one latency probe in seconds, and the load right after it | A slow probe while the load is low points to the engine. A fast probe means the engine is not the cause. |
| Exchanges (today) | The last 12 rows of `exchanges_<date>.jsonl` with their timing strings; the 3 slowest are flagged | `ttfa`/`gen` = time to generate speech, `stt` = time to transcribe it; `play`/`record` = length of the speech itself. |
| Debug log | The last 20 distinct lines of `~/voicemode_debug.log` matching `PaErrorCode`, `Wait timed out`, `underrun`, `timed out`, `stall`, `Falling back to fixed duration` | See below for the fixed-duration fallback. |
| Suspects | Only measured values over a threshold written in the code | These are things to look at first. They are not causes. |

### Suspect thresholds (constants in `stall_diagnose.py`, each one tested)

| Suspect | Threshold |
|---|---|
| CPU load | 1-min load > `LOAD_PER_CORE_SUSPECT` (2.0) x cores |
| Engine down | a configured engine (its port appears in `VOICEMODE_TTS/STT_BASE_URLS`) does not answer `/health` |
| Slow engine | TTS probe > `TTS_PROBE_SLOW_S` (5 s), STT probe > `STT_PROBE_SLOW_S` (5 s), or the probe fails |
| Slow turn | an exchange in the last hour with `ttfa` > 10 s or `stt` > 15 s |
| Audio error | a `PaErrorCode` line in the last hour |
| Fixed-duration fallback | `Falling back to fixed duration recording` in the last hour |
| Stuck thread | a voice-mode process or thread in state `D` |
| Suspended device | a sink-input or source-output on a SUSPENDED sink or source |
| Windows host busy (WSL, WARN) | host CPU load >= `HOST_CPU_SUSPECT_PCT` (90 %) |
| Windows process (WSL) | one process other than `vmmem`, `vmmemWSL`, `wsl`, `wslhost`, `wslservice`, `wslrelay` uses > `HOST_PROC_SUSPECT_PCT` (25 %) of the whole host |
| Windows audio (WSL) | `Audiosrv` or `AudioEndpointBuilder` not `Running` (or not found) |
| Slow PowerShell (WSL) | the `powershell.exe` call took > `POWERSHELL_SLOW_S` (10 s), a timeout included |

The Windows host thresholds are first settings, not derived from measured stalls either.
Measured on this machine on 2026-11-19 at WSL load 22-24 with the host at 100 % CPU: a trivial
`powershell.exe` call took 3.5 and 3.7 s; the host section took 6.0, 7.1 and 12.2 s, and one
earlier run hit the 20 s timeout (n=1, cause not found: stdin was ruled out by rerunning).
So under host overload the section can time out exactly when it is wanted; the timeout then
counts as a Slow PowerShell suspect.

**The probe thresholds (5 s) are not measured.** They are set well above what a GPU engine
should need for one word, but no warm baseline was measured on this machine, because the live
engines were not probed while this was built. After a few runs on a calm machine, replace them
with 10 x the measured warm time.

## A stall mechanism found while building this (n=1)

Validated on 2026-11-18 by reading `~/voicemode_debug.log` around the 22:24 slow turn:

```
22:24:51.815 Recording with silence detection (max 120.0s)...
22:24:53.005 Recording with VAD failed: Error starting stream: Wait timed out [PaErrorCode -9987]
22:24:53.374 Falling back to fixed duration recording
22:24:53.375 Recording audio for 120.0s...
22:26:35.533 Converse cancelled by client (ESC or tool-call cancel)
22:27:13.442 Recorded 2880000 samples          (= 120 s at 24 kHz)
```

How upstream voice-mode 8.12.0 handles this (`voice_mode/tools/converse.py`):
- When the VAD recorder fails, it re-initialises PortAudio (`sd._terminate()` / `sd._initialize()`)
  only if the error text contains `device unavailable`, `device disconnected`, `invalid device`,
  `unanticipated host error` or `portaudio error` (around line 1829). `Wait timed out
  [PaErrorCode -9987]` contains none of these.
- So it falls back to `record_audio(max_duration)` (around line 1869), which records the whole
  120 s **without silence detection**. Only our pill's Pause ends that loop early (line 1326);
  the control channel's stop does not.

In that case the turn cannot end before 120 s, however soon the user stops talking. This is one
stall with a traced mechanism. It does not explain the other stalls. The `Fixed-duration fallback`
suspect exists to count how often it happens.

## A "Restart voice" button in the pill: researched, not built (2026-11-18)

The question was whether a button in the pill could do what `/mcp` reconnect does. Findings
(V = read in the code or measured, I = inferred):

- **Who owns the voice-mode process.** V: a voice-mode MCP server's parent is the `claude`
  process itself, talking over stdio (`ps`, 2026-11-18: 13 of 15). The other 2 had `/init` as
  parent: orphans whose Claude had gone, still alive after 1 d 09 h (not investigated further). V: upstream's only recovery for a
  dropped server is `voicemode reconnect` (`voice_mode/reconnect.py`, VM-1727). It sends
  keystrokes to the `/mcp` menu through tmux, because "its MCP tools vanish" once the server
  exits. Tmux is ruled out for this project. I: Claude Code does not restart a stdio server that
  exits (its source is closed; not tested against the live Claude). So "kill the process"
  (Level 3) would leave the session with no voice. **Out.**
- **The control channel cannot reach a session between turns.** V: upstream opens the control
  socket only inside the audio operation and closes it at the end
  (`converse.py` `_control_listener_scope`; `control_socket.py` header: "bind only while an audio
  operation is active"). There is one well-known socket, owned by the session that is speaking.
  A "re-initialise audio between turns" command sent over the control channel (Level 1) would
  find nobody listening. During a turn, `sd._terminate()` would tear down the stream that is in
  use (I: unsafe, not tested).
- **Engines are not what the reconnect resets.** V: the engine servers are children of `/init`,
  not of any Claude session. They were 12.8 h old while the reconnected session's voice-mode was
  15 min old (`ps`, n=1 reconnect). So restarting the engines (Level 2) is a different action
  from `/mcp`, and `voicemode-switch ensure` already does it.
- **Every pill window would pass the new command on.** V: Windows forwards any `{"cmd":...}`
  line to the backend (`pill-windows.ps1` `OnPageMessage`); macOS does the same
  (`voice_pill_mac.py` `from_page`); the backend dispatches in `voice_pill.py` `handle_command`.
  So delivery was never the problem. There is simply nothing safe at the other end.
- **The one stall traced so far** (above) is a fixed 120 s recording after `PaErrorCode -9987`.
  I: the pill's existing Pause already ends that loop (`converse.py` line 1326, patched).

These would change the decision:
- Diagnose reports taken during stalls that show an engine probe slow or failing at normal load,
  and recovering after `ensure`: then a Level 2 button is worth building.
- Py-spy dumps that show a session stuck between turns inside PortAudio, recovering after a
  re-initialisation: then a per-session request file, read by the session at the start of its
  next turn (as `paused.json` is read), would work where the socket cannot.

## What was NOT tested

- **A real stall.** The command was built and tested with fakes, plus one read-only run on this
  WSL2 machine at load 17 that was not a stall. That run had the engine probes pointed at a dead
  port, skipped the conch CLI and wrote the report to a scratch folder. The live engines were not
  probed.
- The real `voicemode-switch diagnose` writing into the real `~/.voicemode/logs`.
- `py-spy dump` against a real process (py-spy is not installed here).
- macOS (the `ps -axo` / `etime` path) and native Windows (most sections say `not collected`).
- The Windows host section during a real stall. It was run read-only four times on this WSL2
  machine on 2026-11-19 (see the thresholds above), not as part of a whole report. Whether a
  timed-out `powershell.exe` keeps running on the Windows side after the WSL side is killed was
  not checked; its script only reads, and finishes within seconds.
- The host section on another Windows build or with Windows PowerShell missing from PATH
  (that case is a `failed: FileNotFoundError` line in the tests).
- Linux with PipeWire's `pactl` (the output format is assumed to match PulseAudio's).
