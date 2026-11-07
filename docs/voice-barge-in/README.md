# Voice barge-in: stop the assistant when you talk over it

Opt-in. Upstream `converse` has no equivalent as of 9.0.2 (issue #211 is open; its
`mouth`/`listen` packages have barge-in, but nothing connects them to `converse`).

```bash
voicemode-switch barge on       # downloads the Silero model, starts the trigger
voicemode-switch barge off      # takes effect immediately
voicemode-switch barge status
```

From inside Claude Code, Copilot or any MCP client, use the `voice_barge` tool with `on`,
`off` or `status`. `ensure` adds it to `VOICEMODE_TOOLS_ENABLED` if you have a whitelist.

`barge on` also sets `VOICEMODE_CONTROL_CHANNEL_ENABLED=true`: the trigger's `barge_in`
travels over the control channel, and with it off (upstream's default) barge-in is inert.
Validated on native Windows, 2026-11-06: 0 cuts without the channel, 2 of 2 with it. `barge off`
leaves the channel on, because the pill and push-to-talk use it. `barge status` shows its state.

Voice-mode processes started before the patch ignore it, so run `/mcp` to reconnect.

For a program that needs the state without a shell (FELIX reads it at voice start):
`python -m voice_mode.voice_barge status` prints one line, `on`, `off` or
`cannot run: <reason>`, and exits 0. It reads `VOICEMODE_VOICE_BARGE` from
`~/.voicemode/voicemode.env` (upstream's home, whatever `VOICEMODE_BASE_DIR` says), and
from the environment when the file does not set it. It says `cannot run` only where the
interpreter has no mic reader: `parec` (Linux/WSL) or `sounddevice` (Windows and macOS).
The pill shows its "Interrupt by voice" switch when the in-process check passes, else when
the trigger's own python (`VOICEMODE_BARGE_PYTHON`, environment only, else `.venv` beside
`voicemode-switch`) answers `on` or `off` to this command; it asks once per backend run.

## How it works

1. `patch_voice_barge.py` adds `speaking` and `listen_started_at` to the control
   channel's status reply, and a `barge_in` command that acts only while TTS plays.
   Plain `skip_forward` with nothing playing would end the next turn; `barge_in`
   checks under the state lock, so a late detection cannot.
2. `voice_barge.py` is one process per machine. It opens the mic when a voice turn's
   audio starts (the control socket answers, which is only after the conch is taken) and
   closes it when the first reply stops playing, when the server starts listening, or when
   the turn ends; then it stays closed until the next turn. Windows read before the reply
   plays are thrown away: they only let the mic start up while the reply is generated. It
   fires when Silero and a volume gate agree for 192 ms, and only while `speaking`.
3. On a voice cut, `converse` skips the pause and chime. The trigger hands over the last
   3 s of mic audio up to the moment recording starts, trimmed by timestamp, and
   `converse` prepends it. The file (`~/.voicemode/barge_preroll.bin`) is mode 0600 and
   deleted after use.

Silero alone is not enough on speakers: it correctly calls the assistant's echo speech.

| Setting | Default | Notes |
|---|---|---|
| `VOICEMODE_VOICE_BARGE_GATE_DB` | -40 | Raise it (-35) if speech cuts itself off on speakers |
| `VOICEMODE_VOICE_BARGE_MIN_SPEECH_MS` | 192 | 96 ms gave false triggers from echo at lower gates |
| `VOICEMODE_VOICE_BARGE_ECHO` | `auto` | macOS only: `auto` uses echo cancellation on the built-in speakers, `on` always, `off` never (see below) |
| `VOICEMODE_VOICE_BARGE_WARM` | on | `off` opens the mic only once the reply plays (the old behaviour): the first 0.2-1 s of each reply cannot be interrupted. On needs a server that reports `listening` (patch_voice_pill); without it the trigger behaves as `off` |
| `VOICEMODE_VOICE_BARGE_DEVICE` | `@DEFAULT_SOURCE@` | PulseAudio source. On Windows and macOS a device number, or part of a device name. A name that matches several (one per host API) picks the match on the default input's host API, else the first; the choice is logged once. An unknown name is one error line and a non-zero exit at start |

Each speaking stretch (mic open while the assistant speaks) logs one INFO line when it
closes, in `/tmp/voice-barge.log` on Linux and WSL: `stretch: 4.2 s, windows 120, max -41.3 dBFS,
p90 -52.0 dBFS, fired no, first_window 0 ms, deaf 31 ms, warm 1180 ms`. The levels are the echo
to size the gate against. No audio is saved. The timings run from the moment the trigger saw
`speaking`: `first_window` to the mic's first audio, `deaf` to the first window the detector
heard (after the 192 ms warmup), and `warm N ms` says the mic had already been open that long
(`cold`: it was opened for this stretch, as with `VOICEMODE_VOICE_BARGE_WARM=off`).

### Mic start-up: why the mic opens before the reply plays

Before 2026-11-07 the mic opened only when `speaking` showed, so each reply began deaf for
the poll (up to 50 ms), the mic's start-up and the warmup (192 ms):

| Where | Before (cold) | After (warm) |
|---|---|---|
| macOS, reported, plain and voice processing | `first_window` 801-923 ms (n=6), so deaf about 1 s | not measured yet: read `deaf` in the stretch line |
| Windows laptop, Jabra headset, MME, real mic, fake server, 1 s generation (2026-11-07) | deaf median 612 ms, 332-2003 (n=8) | deaf median 58 ms, 3-628 (n=8) |
| Fakes only, 1 s generation, mic start-up 0 / 164 / 850 ms (n=5 each) | deaf median 240 / 408 / 1088 ms | 28 / 30 / 79 ms |
| Fakes only, 0.3 s generation, mic start-up 850 ms (n=5) | 1083 ms | 784 ms: generation shorter than start-up only partly hides it |

The Windows maxima (2003 and 628 ms) are the first stretch of the process, which paid for
a cold `import sounddevice` (1063 ms measured alone, n=1); the trigger now imports it at
start. On Windows alone, opening the stream took about 50 ms and its first audio about
164 ms (median, n=10). Generation time is the margin: the warm mic helps only as far as
generating the reply takes longer than the mic's start-up plus the warmup.

## macOS: echo cancellation on the built-in speakers

Plain capture on a MacBook's built-in speakers hears the assistant at the level of speech
and cuts it off (2 of 2, FELIX-MAC, reported 2026-11-06). So on macOS the trigger reads the
default output at the start of every speaking stretch (CoreAudio through ctypes, three
property reads) and picks the mic for that stretch:

| Output route | How it is detected | Mic |
|---|---|---|
| `headphones` | built-in with data source `hdpn`; any Bluetooth transport (`blue`, `blea`); or another external transport (USB, ...) whose output device also has input streams: a headset | plain capture (sounddevice) |
| `speakers` | built-in with data source `ispk`, any other, or unreadable; HDMI, DisplayPort, AirPlay (`hdmi`, `dprt`, `airp`); or another external transport with no input streams: USB speakers | macOS voice processing (AVAudioEngine, echo cancellation, other-audio ducking at its minimum) |
| `unknown` | CoreAudio did not answer, including a USB/other device whose streams cannot be read | plain capture |

Bluetooth is decided by transport alone, before any stream rule. Measured on FELIX-MAC
2026-11-07: a Jabra Engage 75 appears in CoreAudio as **two** devices, "1 in, 0 out" and
"0 in, 2 out", transport `blue`. The input-stream rule of a2ef7e3 looked at the output
device, found 0 inputs and said `speakers`, so a headset got voice processing (at 7c81771
it was `headphones`, capture sounddevice, 221 windows). A Bluetooth speaker without a mic
is now `headphones` too, so it gets plain capture. For USB and other transports, input
streams are read with `kAudioDevicePropertyStreams` in the input scope of the default
output device (one more property read).

Voice processing needs `pyobjc-framework-AVFoundation` in the trigger's venv
(`./install-mac.sh --barge` installs it). Without it the speakers are **refused**: the
mic stays closed for that stretch and one line is logged per route change,
`barge: built-in speakers without echo cancellation; use headphones
(pyobjc-framework-AVFoundation missing)`. If voice processing fails to start, the
stretch is refused the same way; it never falls back to plain capture on the speakers.
That failure is remembered for the route: no retry until the route changes or the trigger
restarts. Every refused stretch still logs its stretch line, ending
`capture=refused (pyobjc-framework-AVFoundation missing)` or
`capture=refused (echo cancellation failed to start)`.

`VOICEMODE_VOICE_BARGE_ECHO` overrides the rule: `auto` (default) as above, `on` always uses
voice processing (refused without AVFoundation), `off` never does. Voice processing takes
the system default input and ignores `VOICEMODE_VOICE_BARGE_DEVICE`.

On macOS the stretch line ends with the choice after the timings, e.g.
`..., fired no, first_window 0 ms, deaf 12 ms, warm 1400 ms, route=speakers, capture=voice-processing`.
With a cold stretch, `first_window` is the mic's start-up time. `python -m
voice_mode.voice_barge status` prints a second line, `route: speakers, capture:
voice-processing` (or `capture: refused`, or `capture: refused (echo cancellation failed to
start)` when the running trigger recorded that in `~/.voicemode/barge_vp_failed.json`), and
`voicemode-switch barge status` (so the `voice_barge` tool) shows that line too. The first
line and its meaning are unchanged, and the pill reads only the first.

Closing a voice-processing stretch stops the engine, turns voice processing off again
(best effort) and removes the tap. A tap that keeps failing logs one traceback, then a count
at close. `VOICEMODE_VOICE_BARGE_VP_CHANNEL_PROBE=1` logs every tap channel's level at the
end of each voice-processing stretch (`vp channel probe: ...`), to settle which channel is
the processed one.

Not verified yet (no Mac in this repo's test runs; everything above is tested with fakes):

- **Channel 0.** The spike's tap was 48 kHz with 9 channels; it read channel 0 and the echo
  was gone, but nothing says channel 0 is the processed signal (`VP_CHANNEL`). A macOS run
  with `VOICEMODE_VOICE_BARGE_VP_CHANNEL_PROBE=1` (reported, not run here) measured all 9
  tap channels at the same level, -51.2 dBFS: they look like copies of one signal, not a
  processed channel beside raw mics. So channel 0 is echo-cancelled whichever channel is
  "the" processed one, and `VP_CHANNEL = 0` stands. Equal levels are not proof of identical
  samples; a per-channel correlation would settle it if it ever matters.
- **Ducking.** Set to the minimum. Robert heard no dip on Kokoro (n=1); `say` did dip.
  With macOS 13 or older, or if pyobjc will not convert the setting, the level cannot be
  set (logged once, `ducking not settable`) and macOS's default ducking applies.
- **A real trigger run with voice processing.** The spike was a separate recorder, not this
  trigger: silent stretches and a talk-over through it are still to be run, with the
  resampler (proper low-pass, handles 44.1 kHz) and pyobjc's buffer access as written here.
- **Start-up cost.** Reported at 801-923 ms (n=6) for both captures. The mic now opens
  while the reply is generated, so that time is hidden when generation is longer; the
  `deaf` field says how much was left. Whether starting voice processing while the server
  is about to open its own recording disturbs that recording (it already overlapped by up
  to 100 ms before) is untested.
- **The headset rule.** Bluetooth: measured wrong on one headset (above) and replaced by
  the transport rule. USB: that a USB headset reports its mic as input streams on the same
  CoreAudio device as its output is still from the CoreAudio model, not from a device
  (fakes only).

### Diagnosing "windows 0" on voice processing

Reported on FELIX-MAC 2026-11-07 (checkout ad2eb0d): five voice-processing stretches in a
row, 19:09-19:16Z, logged `windows 0 ... first_window none`, warm and cold; then at 19:22Z
the **same trigger process** got 227 windows and cut a talk-over. Between them the Jabra
was switched off. A separate script opening the same capture got 60/60 windows (n=2). The
cause is not known yet. These lines exist to find it:

- Every voice-processing stretch line ends with
  `taps N, samples M, engine_running yes|no (after start yes|no), first_tap T ms|none,
  config_changes K`, read just before the capture closes. `taps 0` with
  `engine_running yes` means the engine runs but its input tap never fires; `engine_running
  no` means it stopped (or never ran); `config_changes` counts
  `AVAudioEngineConfigurationChangeNotification` for this engine (macOS posts it when the
  device under an engine changes, and the engine stops). Each such notification is also an
  INFO line, `voice processing 0x...: engine configuration changed`.
- `VOICEMODE_DEBUG=true` (environment, else `~/.voicemode/voicemode.env`) now sets the
  trigger's log level to DEBUG; before, it was hard-coded INFO. At DEBUG:
  `capture open: <class> id=0x..., phase warm|speaking` and `capture close: ...` per
  capture, so a second open is visible; `voice processing 0x...: engine started, running
  yes, 9 ch at 48000 Hz`; and `phase <phase>: <reason>` at each change of the phase tracker,
  e.g. `phase idle: spoken already in this converse` or `phase idle: status has no
  'listening' ...`, which says why a stretch was cold with warm on.

Ruled out on Linux with a model, not on a Mac: `drain()` at the warm-to-speaking switch
racing the audio thread, and the loop starving a producer thread (a threaded fake capture
delivers through warm, drain and speaking).

Steps for a real Mac: `tests/manual/mac_barge_vp_check.md`.

## Measurements (2026-11-04, one person, WSLg, n=1 per row unless stated)

| Setup | Test | Result |
|---|---|---|
| Laptop speakers + mic | Normal count, before the handoff | 1 of 4 words |
| Laptop speakers + mic | Normal count, at -35 dBFS and 1 s lookback | 10 of 10 words; listen began 28 ms after the cut (log lines, not stream open) |
| Laptop speakers + mic | Soft count, -40 dBFS | 8 of 8 words, cut in about 1 s; voice peaked -15 to -31 dBFS |
| Laptop speakers + mic | Silent during TTS | No cut in 19 s and in 13.4 s (2 runs); echo stayed below the gate, so this does not test echo rejection |
| Headset | Talk over TTS | Fired about 0.9 s into playback; "8, 1, 2" kept. Most of the count predated TTS |

`barge_in` to the player's stop log line: 7 to 35 ms (n=3). Time to actual silence not measured.

## Known limits

- **Strong echo is untested live.** Echo on this laptop swung about 20 dB between runs for
  an unknown reason (median near -62 dBFS, later -82, same devices, both players). The
  -40 gate was only replayed against the loud recordings: 0 false triggers in 3.
- **A real whisper on speakers probably won't interrupt.** A stretch at -41 to -50 dBFS
  was missed under load, and sits at or below the gate.
- **Heavy load** was present in the headset and soft runs. Transcription slowed (see
  `VOICEMODE_STT_TIMEOUT_LOCAL`). Its effect on cut timing has not been measured.
- **Streaming.** An earlier version of this page said the trigger stays inert with
  `VOICEMODE_STREAMING_ENABLED=true`. That is contradicted: on macOS with the setting unset
  (default true) barge cut playback 3 times (FELIX-MAC, reported 2026-11-06, not validated
  here: 2 self-cuts on speakers, 1 talk-over on a Bluetooth headset). WSL with streaming has
  not been re-tested, so treat the old claim as unproven there rather than as fact.
- **Needs the control channel** (see above); native Windows and macOS need it set explicitly.
- **Speakers need an echo-cancelled mic; headsets work.** Results by setup, 2026-11-06:

  | Setup | Result | Status |
  |---|---|---|
  | Windows, Jabra headset | 2 of 2 cuts at -17 / -20 dBFS | validated, n=2 |
  | Windows, laptop speakers + Intel mic array | 0 of 2 self-cuts; 1 of 1 talk-over at -35.4 dBFS | validated, n=2 and n=1 |
  | Windows, laptop speakers + Aukey (no driver processing) | 3 of 3 self-cuts at -13.8 / -31.9 / -33.0 dBFS | validated, n=3 |
  | Mac, built-in speakers | 2 of 2 self-cuts | reported (FELIX-MAC), n=2 |
  | Mac, Bluetooth headset | 0 of 2 self-cuts; 1 of 1 talk-over at -18.1 dBFS | reported (FELIX-MAC), n=3 |

  A mic with no echo cancellation (the Aukey) hears the speakers at the level of speech, so
  no gate separates them. Use headphones, or a mic whose driver cancels echo.
- Needs a mic reader: `parec` on Linux/WSL, `sounddevice` on Windows and macOS. macOS capture
  was validated by FELIX-MAC (9 runs, one machine). The WSL gate numbers above are from one person.
- A 1 s pause ends the turn, as in any listen.
