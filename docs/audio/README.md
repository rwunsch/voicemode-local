# System Audio → Voice Input ("fold what you hear into the mic")

Let voicemode optionally transcribe **your microphone *plus* the computer's
output audio** — e.g. a colleague's voice in a Teams/Zoom call — and toggle that
on and off on demand (CLI or by asking the assistant mid‑conversation).

- **Status:** Linux backend complete and tested. WSL/Windows backend code complete; needs a one‑time VoiceMeeter setup on Windows. macOS is best‑effort/manual.
- **Code:** `voicemode_sysaudio/` (package), `voicemode-switch sysaudio` (CLI), `voicemode_sysaudio.mcp_server` (MCP tool).

---

## The core idea (and the one hard constraint)

voicemode records from **one input device** (the default audio source). To make
that device contain *mic + system output*, we build a **mix** and point voicemode
at it. Where the mix is built depends on where the audio is produced:

> **Hard rule:** you can only fold in audio that exists in the *same audio graph*
> as the recorder. An app's output is captured via that audio system's
> "monitor"/loopback of the speaker. If the app runs in a *different* graph (a
> **Windows** app while voicemode runs in **WSL**), its sound never reaches the
> recorder's graph, so the mix must be built on the **app's** side first.

This single rule explains every per‑OS difference below.

```
            ┌─────────────────────────── one capability ───────────────────────────┐
            │  set_system_audio(on | off | status | setup | teardown)               │
            └───────────────┬───────────────┬───────────────┬───────────────────────┘
                  detect platform → dispatch to backend
        ┌───────────────────┼───────────────────┼───────────────────┐
     linux                 wsl                windows              darwin
  pactl null-sink     Windows VoiceMeeter   VoiceMeeter Remote   BlackHole +
  + module-loopback   helper over interop   API (ctypes)         device switch
  (no extra software)  (Strip[N].B1 = 0/1)  (Strip[N].B1 = 0/1)  (best-effort)
```

---

## Per‑OS wiring

### Linux (native desktop) — cleanest, no third‑party software

PulseAudio/PipeWire expose every output sink's `.monitor` as a capture source
("what's going to the speaker"). We create a null sink and feed it from the mic
(always) and the speaker monitor (toggled):

```
null sink  voicemode_mix
   ├── module-loopback   source=<real mic>          sink=voicemode_mix   (always)
   └── module-loopback   source=<output>.monitor    sink=voicemode_mix   (on/off)
voicemode records  voicemode_mix.monitor
```

- The speaker tap is **non‑destructive** — you still hear the audio normally.
- Per‑app capture is possible: route just one app to a dedicated sink and tap that.
- Setup once, then toggle:

```bash
./voicemode-switch sysaudio setup      # create the mix; default source -> voicemode_mix.monitor
# (restart voicemode so it records the new default source)
./voicemode-switch sysaudio on         # fold system output in
./voicemode-switch sysaudio off        # mic only
./voicemode-switch sysaudio status
./voicemode-switch sysaudio teardown   # remove the mix, restore default source
```

### WSL2 (voicemode in WSL, apps on Windows) — needs VoiceMeeter on Windows

WSLg bridges only two devices: `RDPSource` (the Windows **default mic**, in) and
`RDPSink` (WSL's **own** playback, out). A Windows app's audio (Teams) is **not**
visible in WSL. So the mix is built on Windows with **VoiceMeeter**, exposed as
the Windows default recording device — which WSL then receives as `RDPSource`.

```
Windows:  mic ─────────────┐
          Teams speaker → "VoiceMeeter Input" strip ─┤→ bus B1 ("VoiceMeeter Out B1")
                                          (also → A1 so you still hear it)
          Windows default recording device = "VoiceMeeter Out B1"
                                   │
WSLg RDP bridge ───────────────────┘
WSL:      RDPSource = that mix  →  voicemode records it
```

The on/off toggle flips the Teams strip into/out of bus **B1** via the
**VoiceMeeter Remote API** (`Strip[N].B1 = 1.0/0.0`). Because that API is a
Windows DLL, WSL calls a small Windows helper through interop:

```
WSL: voicemode_sysaudio (wsl backend)
   └─ subprocess → <win_python> <helper_path C:\...\vm_sysaudio.py> on
                      └─ ctypes VoicemeeterRemote64.dll: Login → SetParameterFloat("Strip[N].B1", 1.0) → Logout
```

Flipping the strip changes what `RDPSource` *contains* live — no device switch,
no voicemode restart. One‑time Windows setup is run by a separate Windows Claude
session (see the prompt in the project notes); it installs VoiceMeeter, wires the
routing, drops `vm_sysaudio.py`, and reports the values you put in the config
file below.

### Windows (native voicemode)

Same VoiceMeeter wiring; the toggle calls the Remote API **locally** via ctypes
(`voicemode_sysaudio/backends/windows.py`) instead of over interop.

### macOS — best‑effort (manual)

macOS blocks system‑output capture without a virtual driver. Install
**BlackHole**, create a **Multi‑Output** device (real speakers + BlackHole, so
you still hear audio) and an **Aggregate** device (mic + BlackHole) for capture,
and toggle with `SwitchAudioSource`. The backend currently reports these steps
rather than auto‑toggling.

---

## Configuration

Defaults are sensible for Linux. WSL/Windows need values from the Windows setup,
read from **`~/.voicemode/sysaudio.json`** (env vars `VOICEMODE_SYSAUDIO_*`
override it). Example after the Windows setup reports back:

```json
{
  "win_python": "/mnt/c/Users/you/AppData/Local/Programs/Python/Python312/python.exe",
  "helper_path": "C:\\Users\\you\\voicemode\\vm_sysaudio.py",
  "vm_dll": "C:\\Program Files (x86)\\VB\\Voicemeeter\\VoicemeeterRemote64.dll",
  "vm_strip": 3,
  "vm_bus": "B1"
}
```

| Field | Used by | Meaning |
|-------|---------|---------|
| `mix_sink` | linux | null‑sink name voicemode records (`.monitor`) |
| `mic_source` / `capture_monitor` | linux | override mic / system‑output source (default: system defaults) |
| `win_python` | wsl | Windows Python that runs the helper (referenced by `/mnt/c/...`) |
| `helper_path` | wsl | **Windows** path to `vm_sysaudio.py` (`C:\...`) |
| `vm_dll` | windows | path to `VoicemeeterRemote64.dll` |
| `vm_strip` | wsl/windows | VoiceMeeter strip index carrying system/Teams audio |
| `vm_bus` | wsl/windows | recording bus toggled (default `B1`) |

---

## How to switch it — from the terminal or from a Claude session

### Terminal (any OS)
```bash
./voicemode-switch sysaudio on|off|status|setup|teardown
```

### From inside a Claude/voice session (MCP)

Register the standalone MCP server once:
```bash
claude mcp add voicemode-sysaudio -s user -- \
  /home/you/git/voicemode-local/.venv/bin/python -m voicemode_sysaudio.mcp_server
```

It exposes a single tool, **`system_audio(state)`** (`on|off|status|setup|teardown`).
Then just **ask the assistant** mid‑conversation. Phrases that trigger it:

- "**Fold in the Teams audio**" / "include what I'm hearing" / "capture the system sound" → `on`
- "**Stop including the system audio**" / "mic only now" → `off`
- "**Is the system audio being captured?**" / "system‑audio status" → `status`

The assistant calls `system_audio` with the right state and confirms briefly. It
won't toggle on its own — only when you ask.

---

## Caveats

- **Echo / feedback:** when system audio is folded in, voicemode's own TTS — if it
  plays to a captured output — gets re‑recorded (and a Teams colleague may hear it).
  **Use headphones**, or route TTS to an output that isn't captured.
- **Teams mic:** on Windows, pin Teams' *microphone* to your real mic, not the
  VoiceMeeter mix, or the colleague hears themselves echoed.
- **WSL only sees Windows‑side audio** once VoiceMeeter is set up and "VoiceMeeter
  Out B1" is the Windows default recording device.

---

## Reproduction / tests

```bash
.venv/bin/pytest tests/test_sysaudio.py -q          # 19 unit tests (detect/config/backends)
# Live Linux mechanism check (creates + tears down the mix; default source untouched):
PYTHONPATH=$PWD .venv/bin/python -c "import voicemode_sysaudio as s; print(s.set_system_audio('on', kind='linux'))"
pactl list short modules | grep voicemode_mix
PYTHONPATH=$PWD .venv/bin/python -c "import voicemode_sysaudio as s; print(s.set_system_audio('teardown', kind='linux'))"
```
