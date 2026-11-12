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
| Last Updated | 2026-11-12 (relay token and limits; a tap behind a slow send stays a tap) |
| Project      | voicemode-local, branch feature/native-ptt (task 7 of the native Windows plan) |

## Keys (both modes)

| Key | What is sent |
|-----|--------------|
| Ctrl+Space, key down | `hold_start` at once: the reply is cut (also while still being generated) and the mic opens |
| released after 800 ms or more (hold) | `hold_end`: what you said is transcribed and answered |
| released within 800 ms (tap) | `stop` with hint `switch-to-text`: full stop, the recording is dropped, the agent continues in text |

The 800 ms is `$TapMaxMs` in the script and `TAP_MAX_S` in `patches/ptt_core.py`, the same on
every platform (Robert, 2026-11-07). The script sends the wire words `hold_start`,
`hold_release` and `short_press`; turning `short_press` into `stop` is the sender's (or, in
relay mode, `ptt_relay`'s) job, in `ptt_control_client.COMMANDS`. `-HoldThresholdMs` is still
accepted so old shortcuts keep starting, and ignored.

### A hold with no voice turn running wakes the last voice session (2026-11-07)

Between voice turns there is no control channel (`no-channel`). A **hold** (released after
800 ms) then asks the last voice session to start listening, through Claude Code's own
session socket (`patches/session_wake.py`): it sends one fixed message, which the session
shows as "Another Claude session sent a message", asking the agent to call converse with
`message="Listening."` (spoken) and `wait_for_response=true`. A tap never wakes.

**The gesture, honestly (Robert's decision 2026-11-07, "option 1"): an idle hold only
wakes the session.** Hold the key, release, wait until you hear "Listening.", then talk. Words
spoken during the hold are lost by design: the session needs its own time to wake and call
converse. Measured live 2026-11-07, wake to converse listening: 6 s and 11 s (n=2; the model
and the tool lookup). The wake message makes the session speak "Listening." before it listens (a silent wake was heard on the Mac 2026-11-07, n=1; the spoken cue on the new message is not yet heard live). A still-held key or a barge suppresses the listen chime. Because the hold had nothing to end
(no voice turn was running when it began), the release sends no `hold_end`; the log says
`woke <label>; talk after Listening.`. The voice pill shows `Waking <label>... listen for "Listening."` for its Wake button only.

- Native: the sender wakes after answering `hold_release` with `no-channel` (the answer
  word is unchanged, so the script needs no change). WSL relay: `ptt_relay.forward` does it.
- Only an **idle** session is woken. Busy, waiting on a permission prompt, running a shell
  command, a stale registry entry, a key or socket that is not private: refused, nothing sent.
- One wake per 5 s. Every attempt is logged to `~/.voicemode/logs/session-wake.log`
  (in WSL for the relay, in the Windows profile for native); the token never is.
- Off: `VOICEMODE_SESSION_WAKE=off` (environment or voicemode.env).
- The protocol is Claude Code's undocumented internal one (read from its binary, validated
  n=1 on WSL, Claude Code 2.1.319). Native Windows uses a named pipe there: implemented
  behind the same function with fakes only, **UNVERIFIED** on a real Windows Claude Code.
- A session whose `crossSessionInbound` setting is `hold` will not act on it. The binary
  also says a session that bypasses permission prompts holds a message whose sender did not
  attest its permission mode; we attest nothing, so a bypass-mode session may hold the wake
  (not tested).

The Mac keeps its own wake (typing into Terminal), see docs/macos-push-to-talk.

Only while a terminal process has focus (`WindowsTerminal`, `powershell`, `pwsh`, `wsl`,
`conhost`, `cmd`, `alacritty`, `wezterm`, `Code`), so Ctrl+Space in other apps is left alone.
The key states are polled every 50 ms with `GetAsyncKeyState`; nothing is hooked or swallowed.

## Modes

**Relay (WSL2).** No `-SenderExe`. Sends `{"action": "..."}` lines over TCP to
`patches/ptt_relay.py` in WSL (`-RelayHost` 127.0.0.1, `-Port` 8765), probes the relay with
`press` at start, and logs to `\\wsl.localhost\Ubuntu\home\wunsch\.voicemode\logs\ptt-listener.log`
unless `-LogPath` says otherwise.

**The relay needs a token (2026-11-12, Talos review).** Start the relay as before
(`VOICEMODE_PTT_HOST=0.0.0.0 python -m voice_mode.ptt_relay`). On its first start it writes a
shared secret to `~/.voicemode/ptt-relay.token` in WSL (32 random bytes as hex, mode 600) and
reuses it after that. A damaged file is replaced. Every connection must begin with the line
`token <hex>`; a connection with a missing or wrong token is closed and nothing it sends is
forwarded. Without the token, anything that could reach the port could stop speech, hold the
mic open or wake an idle session.

- The listener reads the file over the WSL share, by default
  `\\wsl.localhost\Ubuntu\home\wunsch\.voicemode\ptt-relay.token`, the same WSL home as its
  log. Another distro or user: pass `-TokenPath`. `-TokenPath` with `-SenderExe` exits 2.
- The token is read once, at the first send that finds it, and kept. If the file cannot be
  read, the listener logs `relay token not readable at <path> ...` once and sends nothing
  until it can. Start the relay once to create the file.
- If the relay writes a new token (only when the file was damaged or deleted), restart the
  listener.
- The legacy Python companion `patches/ptt_listener_windows.py` sends no token, so the relay
  refuses it. Nothing installs it; use the PowerShell listener.

Other relay limits: a line longer than 1024 bytes closes the connection (it is read with a
bound, never buffered whole; a 300 MB line with no newline used to grow the relay to 306 MB),
and at most 8 connections are served at once; more are closed at once. The listener's hold
state (did this hold begin with no voice turn) is kept for the whole relay, not per
connection, because the listener opens a new connection for every action. A lock keeps
connections from interleaving it.

**A tap behind a slow send stays a tap (2026-11-12).** Native mode does not wait for the
sender's reply to `hold_start`. The key loop keeps polling, so the release is timed from the
key state. The reply is collected, and its outcome logged, before the next write. Before,
a cold sender that took over 800 ms to answer made a quick tap count as a hold. The key-down
log line then shows `sent=pending`. This was parse-checked and its structure is tested
statically. It was not run with real keys.

**Native.** `-SenderExe <python>` (and optionally `-SenderArgs`, default
`-m voice_mode.ptt_control_client --stdin`). The script starts the sender once, as a hidden
child process, and warms it before reading keys: a `warm` line makes it load the path
resolver and the pipe transport and answer `ready` (logged as `sender ready in N ms`), so
the first press costs what later ones do (the first press took 0.97 s cold, 2026-11-07).
Then for each action it writes the action word and reads one outcome line:

| Outcome | Meaning |
|---------|---------|
| `sent` | the command reached the pipe |
| `none` | nothing to send (`press`) |
| `no-channel` | no pipe: no voice turn is running |
| `busy` | every pipe instance stayed busy past 0.5 s |
| `refused: <why>` | the pipe is not owned by this user, or its server runs as another user or at a lower integrity level |
| `failed: <why>` | anything else; the sender is restarted on the next key if it died |

Keys pressed while the sender is starting or hung (up to 5 s) are lost.

The sender maps the action with `ptt_control_client.COMMANDS`, names the pipe with
`control_transport.pipe_name()` of `VOICEMODE_CONTROL_SOCKET` or the base dir's
`control.sock`, on every send, and checks owner and server before writing
(`control_transport._verify_server`). It inherits the listener's environment, which the
launcher gives the same settings as voice-mode, so both name the same pipe. A reason the key
does nothing is logged once, then again only when it changes. There is no start probe, since
the pipe exists only during a voice turn. `-SenderExe` together with `-Port` or `-RelayHost`,
or `-SenderArgs` without `-SenderExe`, exits 2.

`-ReplayKeys "0:down,500:up"` runs key events through the same rule as the key loop
(`Get-ReleaseAction`) and sends the result, logging `replay <action> sent=...`.

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
  powershell.exe where WSL interop works: parameter mixing, a sender that cannot start, an
  unreadable token logged once, and relay mode end to end against a real relay with a token.
- `tests/test_ptt_relay.py`: the token (right, wrong, missing; the file created once with mode
  600), an over-long line closing the connection at once, the connection limit, and hold state
  across connections, all against a real relay on loopback.
- `tests/test_voicemode_native.py`: the launcher's command, the `ptt` switch, the pipe name.
- `tests/test_ptt_listener_windows_native_windows.py` (Windows): the transport's own server
  on a real pipe receives `stop` (hint `switch-to-text`), `hold_start`, `hold_end` for tap, hold, release, and
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
4. Start a voice conversation. While the assistant speaks, hold Ctrl+Space: the reply stops
   the moment the key goes down; talk, release after a second or more: you are answered.
5. Hold Ctrl+Space, pause mid-sentence for 3 s, release: nothing should cut you off until release.
6. While a reply is still being generated (before any sound), hold Ctrl+Space and talk: no
   reply is played, your words are answered.
7. Tap Ctrl+Space (under 0.8 s) while it speaks: the voice turn ends, the agent carries on in
   text.
8. Read `<root>\run\ptt-listener.log` (`<root>` is `%LOCALAPPDATA%\voicemode-local-native` by
   default): `sender ready in N ms`, `hold_start (mic open) sent=True`,
   `hold_end (sending, N ms) sent=True`, `tap -> stop (N ms) sent=True`. Between turns a press
   logs `no control pipe` once.
9. Tap Ctrl+Space with a browser in front: `ignored: foreground is not a terminal`.
10. `ptt off`: `Stopped the listener (process N)`.
