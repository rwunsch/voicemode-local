# Push-to-talk on Windows: WSL relay and native

> **TL;DR:** `ptt-listener-windows.ps1` reads Ctrl+Space on Windows. With no `-SenderExe`
> it sends to the WSL relay, exactly as before. With `-SenderExe` (native Windows, started
> by `scripts/windows/voicemode_native.py`) it hands each action to
> `python -m voice_mode.ptt_control_client --stdin`, which writes the control command to
> voice-mode's named pipe after checking who owns and serves it. Tested on Windows against
> a fake control server on a real pipe; real keys are untested.

| Field        | Value |
|--------------|-------|
| Status       | In progress: built and tested without keys; a real key test is pending |
| Created      | 2026-11-07 |
| Last Updated | 2026-11-07 |
| Project      | voicemode-local, branch feature/native-ptt (task 7 of the native Windows plan) |

## Keys (both modes)

| Key | What is sent |
|-----|--------------|
| Ctrl+Space, tap (released before 400 ms) | `skip_forward`: cuts the reply, or ends your turn |
| Ctrl+Space, hold (400 ms or more) | `hold_start`, then `hold_end` on release: the mic stays open while held |

Only while a terminal process has focus (`WindowsTerminal`, `powershell`, `pwsh`, `wsl`,
`conhost`, `cmd`, `alacritty`, `wezterm`, `Code`), so Ctrl+Space in other apps is left alone.
The key states are polled every 50 ms with `GetAsyncKeyState`; nothing is hooked or swallowed.

## Modes

**Relay (WSL2), unchanged.** No `-SenderExe`. Sends `{"action": "..."}` lines over TCP to
`patches/ptt_relay.py` in WSL (`-RelayHost` 127.0.0.1, `-Port` 8765), probes the relay with
`press` at start, and logs to `\\wsl.localhost\Ubuntu\home\wunsch\.voicemode\logs\ptt-listener.log`
unless `-LogPath` says otherwise.

**Native.** `-SenderExe <python>` (and optionally `-SenderArgs`, default
`-m voice_mode.ptt_control_client --stdin`). The script starts the sender once, as a hidden
child process, and for each action writes the action word and reads one outcome line:

| Outcome | Meaning |
|---------|---------|
| `sent` | the command reached the pipe |
| `none` | nothing to send (`press`) |
| `no-channel` | no pipe: no voice turn is running |
| `busy` | every pipe instance stayed busy past 0.5 s |
| `refused: <why>` | the pipe is not owned by this user, or its server runs as another user or at a lower integrity level |
| `failed: <why>` | anything else; the sender is restarted on the next key if it died |

The sender maps the action with `ptt_control_client.COMMANDS`, names the pipe with
`control_transport.pipe_name()` of `VOICEMODE_CONTROL_SOCKET` or the base dir's
`control.sock`, on every send, and checks owner and server before writing
(`control_transport._verify_server`). It inherits the listener's environment, which the
launcher gives the same settings as voice-mode, so both name the same pipe. A reason the key
does nothing is logged once, then again only when it changes. There is no start probe, since
the pipe exists only during a voice turn. `-SenderExe` together with `-Port` or `-RelayHost`,
or `-SenderArgs` without `-SenderExe`, exits 2.

`-SendAction short_press,hold_start,...` sends those actions through the same function the
keys use, logs each and exits. The tests use it, and it is the way to check the path without
pressing a key.

## Native: switch it on

```powershell
& <python> <repo>\scripts\windows\voicemode_native.py ptt on      # or off, status
```

`on` writes `VOICEMODE_PTT_ENABLED=true` to `%USERPROFILE%\.voicemode\voicemode.env` and, while
a voice connector runs, starts the listener into the install's services Job Object (one per
install). Otherwise the next connector starts it. The launcher also turns the control channel
on for voice-mode when push-to-talk is on (`VOICEMODE_CONTROL_CHANNEL_ENABLED`); a connector
that was already running without it has to be reconnected (`/mcp`). `off` kills the listener
and its sender. Logs: `<root>\run\ptt-listener.log` (one line per event) and
`<root>\run\ptt.log` (the console, for start-up errors).

## Why the pipe client is in Python, not in the script

Spec section 4 asked for `NamedPipeClientStream` in the script. The first version had a C#
pipe client in the script with the owner and server checks (`OpenProcessToken`,
`GetTokenInformation`, `GetSecurityInfo`, `GetNamedPipeServerProcessId`) beside the
`GetAsyncKeyState` polling. Windows Defender blocked it as "malicious content" on 2026-11-07,
and quarantined a copy written to `%TEMP%`. The unchanged original and the C# alone were not
flagged. A test now fails if pipe or token code comes back into the script.

## Tests

- `tests/test_ptt_control_client.py`: outcomes, and `--stdin` end to end against a real
  AF_UNIX server (Linux).
- `tests/test_ptt_listener_windows_native.py`: the script, statically, and through
  powershell.exe where WSL interop works: parameter mixing, a sender that cannot start.
- `tests/test_voicemode_native.py`: the launcher's command, the `ptt` switch, the pipe name.
- `tests/test_ptt_listener_windows_native_windows.py` (Windows): the transport's own server
  on a real pipe receives `skip_forward`, `hold_start`, `hold_end` for tap, hold, release, and
  nothing for `press`; no pipe is logged once; a Low-integrity squatter of the same user and
  a pipe owned by someone else (the sender told it runs as SYSTEM) are refused before
  anything is written; relay mode still sends `{"action": ...}` over TCP.
- `tests/test_voicemode_native_windows.py` (Windows, `-k ptt`): `ptt on` and `off` twice
  with a running `serve`, listener and sender in the services Job Object; a `launch` with
  push-to-talk on starts it, gives voice-mode the control channel, and leaves nothing behind;
  the runtime's installed sender reaches a pipe named for the test profile's `control.sock`.

## Not tested

Real keys, focus detection and a spoken turn. See the morning check below.

## Morning check (real keys)

1. Native install with the runtime set up (`install.ps1 -Mode windows-native`), Claude Code
   using it as the voicemode MCP server.
2. `& <python> <repo>\scripts\windows\voicemode_native.py ptt on`, then `/mcp` reconnect (so
   voice-mode gets the control channel).
3. `ptt status` shows `on` and `listener: running (process N)`.
4. Start a voice conversation. While the assistant speaks, tap Ctrl+Space in the terminal: the
   reply should stop and recording start. While you speak, tap: your turn should end.
5. Hold Ctrl+Space, pause mid-sentence for 3 s, release: nothing should cut you off until release.
6. Read `<root>\run\ptt-listener.log` (`<root>` is `%LOCALAPPDATA%\voicemode-local-native` by
   default): `short_press sent=True`, `hold_start (mic open) sent=True`,
   `hold_end (sending) sent=True`. Between turns a tap logs `no control pipe` once.
7. Tap Ctrl+Space with a browser in front: `ignored: foreground is not a terminal`.
8. `ptt off`: `Stopped the listener (process N)`.
