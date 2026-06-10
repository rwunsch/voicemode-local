# Windows VoiceMeeter setup prompt

Paste the block below into a **Claude Code session running on Windows** (not WSL).
It installs and wires VoiceMeeter so WSL voicemode can fold Windows system audio
(e.g. a Teams colleague) into the mic, and creates the toggle helper the WSL
backend calls. When it finishes, copy the reported values into
`~/.voicemode/sysaudio.json` in WSL (see `docs/audio/README.md`).

```text
You are helping me set up the Windows side of "voicemode-local". Context:

- voicemode-local is a local voice assistant for Claude Code. On my machine it
  runs inside WSL2 (Ubuntu). WSL captures audio only through WSLg's RDP bridge:
  it sees ONE recording device (the Windows default mic) and cannot see any
  Windows app's audio.
- Goal: voicemode (in WSL) should optionally hear my mic + Windows system output
  mixed together — specifically a colleague's voice from Microsoft Teams — and I
  want to toggle that ON and OFF programmatically from WSL.
- The mix must be built on the WINDOWS side and exposed as the Windows default
  recording device, with a routing switch I can flip remotely. The right tool is
  VoiceMeeter (it has a Remote-control API). I currently have only VB-CABLE,
  which is not enough.

Please do the following, checking with me before any step that changes a
system-wide default or touches Teams settings:

1. Install VoiceMeeter (base edition is fine; Banana OK) from the official
   VB-Audio source. Prefer winget: `winget install VB-Audio.Voicemeeter`
   (or `VB-Audio.Voicemeeter.Banana`). Reboot if needed. Confirm the install
   path and the Remote API DLL (`VoicemeeterRemote64.dll`, usually under
   `C:\Program Files (x86)\VB\Voicemeeter\`).

2. Launch VoiceMeeter; set it to run at Windows startup + minimize to tray
   (the Remote API needs VoiceMeeter running).

3. Wire bus B1 = mic + Teams:
   - Hardware Input strip 1 = my physical microphone; enable its B1 button.
   - A VoiceMeeter virtual-input strip carries Teams: I'll set Teams' SPEAKER
     output to "VoiceMeeter Input". Enable that strip's A1 (so I still hear the
     colleague via my real headphones = Hardware Out A1) AND B1 (recording mix).
   - Report the exact strip INDEX of that virtual-input strip in the Remote API
     addressing (Strip[N]); verify by reading it back via the API.

4. Set the Windows default recording device to "VoiceMeeter Out B1"
   (confirm first — affects all apps using the default mic).

5. In Teams → Settings → Devices, pin the Microphone to my real physical mic
   (NOT "Default"/the mix), so my colleague never hears themselves. Confirm
   before changing Teams settings.

6. Create a parameterized toggle helper at C:\Users\<me>\voicemode\vm_sysaudio.py
   using the VoiceMeeter Remote API (ctypes against VoicemeeterRemote64.dll):
     python vm_sysaudio.py on      -> Strip[N].B1 = 1
     python vm_sysaudio.py off     -> Strip[N].B1 = 0
     python vm_sysaudio.py status  -> print current Strip[N].B1
   VBVMR_Login, set/get float "Strip[N].B1", VBVMR_Logout, non-zero exit on
   error. Make the strip index configurable (CLI arg or a small JSON beside it).
   Use the Windows Python at
   C:\Users\<me>\AppData\Local\Programs\Python\Python312\python.exe.

7. Test it: run status, on, status, off, status; confirm the B1 value actually
   changes (read-back) and that you still hear Teams throughout.

When done, report back EXACTLY (I'll paste this to the WSL side):
- VoiceMeeter edition + install dir + full path to VoicemeeterRemote64.dll
- The Teams virtual-input strip index (Strip[N]) and bus (B1)
- Full Windows path to vm_sysaudio.py and the exact command line to run it
- The Windows Python executable path you used
- Confirmation that "VoiceMeeter Out B1" is the default recording device
- The status/on/off test output
```

## Wiring the WSL side afterward

Put the reported values in `~/.voicemode/sysaudio.json`:

```json
{
  "win_python": "/mnt/c/Users/you/AppData/Local/Programs/Python/Python312/python.exe",
  "helper_path": "C:\\Users\\you\\voicemode\\vm_sysaudio.py",
  "vm_dll": "C:\\Program Files (x86)\\VB\\Voicemeeter\\VoicemeeterRemote64.dll",
  "vm_strip": 3
}
```

Then `./voicemode-switch sysaudio status|on|off` from WSL drives the Windows
toggle, and the `voicemode-sysaudio` MCP tool lets you ask the assistant to do it
mid‑conversation.
