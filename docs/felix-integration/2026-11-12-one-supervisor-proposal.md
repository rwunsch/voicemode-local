# Proposal: one shared voice supervisor and runtime builder

Status: **APPROVED by Robert, 2026-11-12.** Agreed between the voicemode-local and FELIX-voice
sessions, revision 3. Robert's decisions:
1. The split: yes.
2. Timing: route i. #1743 ships with FELIX's host fix now; the move into the shared module
   follows as its own PRs.
3. Talos keeps reviewing `plugins/voicemode/vendor/**`: no exclusion.

## The problem

- **The origin.** The runtime, service and lease logic was written in FELIX's
  `plugins/voicemode/scripts/voice.py`. voicemode-local copied it into
  `scripts/windows/voicemode_native.py` at FELIX commit aea841ffd (that file's own header,
  lines 4-10) and added Job Objects, the pill and push-to-talk.
- **Both files are now drifted copies of that code.** Measured 2026-11-12, top-level
  functions, difflib per function, n=1 each.
  - FELIX's `voice.py` at #1743 b36f288ce against voicemode-local's `voicemode_native.py` at
    13a26ae: 78 against 101 functions, 46 shared, 32 FELIX-only.
  - Of the 46 shared: 13 near-identical (≥0.95) in both measurements. FELIX-voice's split of
    the rest is 13 similar and 20 diverged; mine is 10 similar and 23 diverged. difflib's
    ratio depends on argument order.
  - Among the diverged: `setup` 0.17-0.18, `runtime_environment` 0.20, `launch` 0.23-0.30,
    `ensure_services` 0.40-0.47.
  - Some of FELIX's versions are newer fixes, not gaps.
- **The scope differs.** voicemode-local's copy is Windows-only (`check_machine`, line 506);
  voicemode-local runs on the Mac through `voicemode-switch` (bash). FELIX's voice.py runs the
  lifecycle on Windows and on the Mac.
- **More copies.**
  - FELIX keeps its own `kokoro_onnx_server.py`: loopback by default, `--model-dir` required,
    never downloads, no German.
  - FELIX keeps a hand-kept `patches/manifest.mac.json`.
- **The live cost** (FELIX-voice, Windows, n=1):
  - The speech services run inside the job of the chat that started them.
  - When the Copilot app unloads that chat (seen after 12.7 min idle), every other chat loses
    speech.

## The split

**One shared Python module in voicemode-local, cross-platform (Windows and macOS).** It is
built by moving FELIX's code, which already runs on both, and adding voicemode-local's
Windows parts (Job Objects, the pill, push-to-talk). It is driven by a **host profile**. The
voicemode-local copy in `voicemode_native.py` retires into it. Two parts:

1. **Runtime builder.** Download and verify; hash-locked `uv sync`; model links; the
   whisper.cpp zip; atomic publish only after a speech check; completeness and repair check;
   the setup deadline (exit 75). The profile supplies the policy: lock file, model list, extra
   files, digest inputs. The builder computes the runtime digest **with FELIX's formula**
   from the profile's inputs, so FELIX's JS `manifestSha` keeps matching. Otherwise a FELIX
   machine would rebuild, or skip a repair, wrongly.
2. **Supervisor.**
   - **Windows design:** one host process started **outside every chat** via
     `Win32_Process.Create` (as FELIX's installer already does). It owns the speech services,
     the barge trigger and the push-to-talk listener, and the named Job Object, as its
     children. Then a chat's job never holds them, whether or not that job allows breakaway.
     Breakaway is only an optimisation once measured.
   - Leases, watchdog (started first), and port checks: the listener must be our process tree,
     and no port is picked twice while a service loads.
   - Stop signals are held until a process is recorded. Status and stop.
   - Every fallback is recorded as a result, never silent.

Also made options of voicemode-local's own files, so FELIX vendors them unchanged:
- the Kokoro server's flags (loopback default, required `--model-dir`, no download, no German);
- a patch-list profile in `apply.py` (no provider keys, no ffmpeg), replacing FELIX's
  hand-kept Mac list.

**FELIX keeps (policy and glue):**
- what to install, and the digest inputs, mirrored in the JS `manifestSha`;
- the fe-bootstrap contract: `active.json`, `.complete.json`, `install-state.json`,
  `last-start.json`;
- `launch.js`, building the environment, the microphone pre-check;
- FE-facing text: `record_start`, outdated notice, the skills;
- the Copilot-only pieces: the `session.send` wake extension and "FELIX speaks first", both
  not built yet.

**Who decides a switch.**
- In FELIX's profile, FELIX's preference (`voice.barge` in `preferences.json`) is the source
  of truth. `VOICEMODE_BARGE_CONTROL_COMMAND` stays FELIX's command: it saves the preference,
  then calls the supervisor's entry point for the process action only.
- voicemode-local's switch, which writes voicemode.env, stays for personal installs. voicemode.env
  is never authoritative for FELIX.

**voicemode-local's other fixes** (FELIX works around them today):
- the conch home follows a setting instead of the home folder fixed at import;
- a setting turns off the config search up the folder tree;
- beeps missing by default (cause unknown);
- the pill label lagging the speaker.

## How we keep it one copy

- FELIX vendors only tagged voicemode-local releases, byte-identical, and never patches a
  vendored file locally.
- A FELIX need goes to voicemode-local as a change request and comes back in a tag.
- FELIX's parity tests run against each vendored tag in FELIX's suite.
- voicemode-local keeps the profile interface stable within a minor version and lists breaking
  changes in its release notes.

## FELIX's conditions, and how they are met

| # | Condition (FELIX-voice) | How |
|---|---|---|
| 1 | Vendored byte-identical at a tag, never fetched | The shared module ships as files at a voicemode-local tag, like the patches. |
| 2 | No provider key names in vendored text | `voicemode_native.py` names none (checked 2026-11-12). A test pins it for the shared module. |
| 3 | No silent fallbacks | Every fallback (breakaway refused, a port taken, a helper failing) is recorded as a result FELIX reads. |
| 4 | Stable interface with fe-bootstrap | FELIX's files stay FELIX-written. The builder publishes what the profile names and never rewrites FELIX's records. |
| 5 | FE-facing text comes from FELIX | The module returns structured results (code, detail). FELIX words them. |
| 6 | No console windows; stdio passes through; FELIX's ports | Already `CREATE_NO_WINDOW`. Ports are a profile value (FELIX: 18880 / 12022 plus `ports.json`). |
| 7 | No ffmpeg, hash-locked, offline after setup, loopback, proxies stripped | Profile values. FELIX's proxy-free loopback HTTP moves into the module. |

## The entry points' inputs (FELIX-voice's list)

| Input | FELIX's value |
|---|---|
| root, run dir | `FELIX_VOICE_ROOT` (default `%USERPROFILE%\.felix\voice`); `<root>\run` |
| runtime and its python | from `active.json` |
| TTS | runtime python, `kokoro_onnx_server.py --port N --host 127.0.0.1 --model-dir ...`; ready on `/v1/models` |
| STT | `whisper-server -m ggml-base.bin --host 127.0.0.1 --port N` (bundled on Windows, Homebrew on Mac); ready on `/health` |
| ports | 18880 / 12022, a range after them, `<root>\ports.json` |
| environment | FELIX's `runtime_environment` output |
| barge | mode, silero model path |
| push-to-talk (Windows) | sender `python -m voice_mode.ptt_control_client --stdin`, `-FocusPattern '^github$'`, log file |
| results back | ports chosen, each part's state, helper problems, a hard error with reason and next step |

## Timing

- **Route i (both sessions recommend it):** #1743 ships now with FELIX's small fix. That fix
  starts the services and helpers in a process created via `Win32_Process.Create`, outside the
  chat's job. It is the first step of the supervisor's Windows design above, not a throwaway.
  The move into the shared module follows as its own reviewed PRs.
- **Route ii:** hold #1743 until the move is done. Slower, and multi-chat voice stays broken
  on FELIX's side meanwhile.

## Order of work after approval

1. **FELIX-voice:** the #1743 fix (route i). Measure, with Robert, whether the Copilot app's
   job allows breakaway: one spawn from a chat's own PowerShell tool, `IsProcessInJob` plus the
   job identity, with a control run.
2. **voicemode-local, test-first:**
   - Move FELIX's lifecycle and runtime builder into a shared cross-platform module, taking
     FELIX's newer fixes.
   - Add the Windows host design and Job Objects, the recorded fallbacks, and the profile.
   - Build parity tests from FELIX's ~210 voice tests (`tests/repo/test_voicemode_setup.py`,
     `plugins/fe-bootstrap/tests/test_voice_opt_in.py`).
   - `voicemode_native.py` becomes a thin personal-install profile over it.
   - Tag a release.
3. **FELIX-voice:** voice.py becomes a thin FELIX profile over the vendored module, in one PR.
4. **voicemode-local:** the Kokoro server options, the `apply.py` patch-list profile, the
   conch home setting, the walk-up setting, beeps, the pill label.

## Questions for Robert

- Approve the split above?
- Route i (recommended) or route ii?
- 27 of Talos's 34 findings today were on vendored files. Should FELIX's repository exclude
  `plugins/voicemode/vendor/**` from Talos? Doing so removes one independent reviewer.
  voicemode-local has no Talos: its reviews are fresh-reviewer passes by Claude sessions.
  The trade-off is a repository setting and your call.

## Not known yet

- Whether the Copilot app's job allows breakaway (step 1). The host design does not depend on
  it.
- Whether Copilot app requests carry a progress token (the CLI's do).
- The cause of the missing beeps. FELIX-voice's next live run turns on debug logging.
