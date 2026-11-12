# Real-hardware checklist: Windows and macOS (v1.1.3)

Everything below has passed only in tests on WSL, some of them through `powershell.exe`.
None of it has run on a real Windows or Mac machine with real keys, a real pill window and a
real voice turn. This list closes that gap. Robert, 2026-11-12: Windows and Mac are in scope.

**How to record.** For each item, write `PASS` or `FAIL` with the date, the version
(`git describe --tags`), the machine and n (how many times it was tried), plus the log line
that shows it. A FAIL gets an issue line here and a fix in the next version. Do not tick an
item from memory.

**Before you start**
- [ ] The voice server runs v1.1.3: run `git -C <repo> describe --tags`, then reconnect with
  `/mcp`.
- [ ] Headphones and speakers are both at hand, because barge-in behaves differently on each.

---

## Windows (native, no WSL)

### W1. Install on a clean machine
Never tested. Use a clean Windows machine; a fresh VM is fine.
- [ ] `install.ps1 -Mode windows-native` with no Python installed: it stops and names
  python.org.
- [ ] With Python but no uv: it prints the uv installer command. `-InstallUv` runs it.
- [ ] A full install finishes. `voicemode_native.py status` lists TTS, STT and the barge
  trigger as running.
- [ ] Defender quarantines nothing: `pill-windows.ps1`, `ptt-listener-windows.ps1` and the
  `.cmd` in the Startup folder are all still there. An earlier listener version was
  quarantined once.

### W2. Pill
- [ ] It appears on the first `converse` and closes about 20 s after the last voice session
  ends.
- [ ] Its phase follows a real turn: speaking, listening, between turns.
- [ ] Pause and Resume work. Stop works during a turn **and while paused** (new in v1.1.3).
- [ ] Wake between turns shows the sunrise, and the woken session says "Listening."
- [ ] Closing the window while paused releases the pause.
- [ ] German is not offered when only English Kokoro is installed. With
  `VOICEMODE_PILL_LANGUAGES=English` it is never offered (v1.1.3).
- [ ] The Ctrl+Space keys show only while the push-to-talk listener runs.
- [ ] `VOICEMODE_PILL=True` (capital T) still opens the window (v1.1.3).
- [ ] Kill the pill backend while it is running: the page shows "Disconnected" at once,
  not after 3 s (v1.1.3).
- [ ] Autostart: `pill-windows.ps1 -InstallAutostart` with your usual arguments, then a
  **real login**. The pill comes up, and the `.cmd` keeps every argument, including a
  non-ASCII path if you have one.
- [ ] Two monitors, a scale above 100 %, a fullscreen app: note what happens. Not handled yet.
- [ ] Voice server elevated, pill not: expect the logged "control channel refused" (a known
  limit).

### W3. Barge-in (talk over the reply to stop it)
- [ ] Headphones, `voice_barge on`: talking over a reply stops it, and your words are
  answered.
- [ ] Speakers, `auto`: no barge, and the first-turn notice says "headphones" (v1.1.3).
- [ ] Speakers, `on`: does the reply's own echo trigger it? Strong echo is untested live.
- [ ] Unplug and replug the headset during a session: barge works again on the next reply
  without a restart (v1.1.3).
- [ ] Barge, then press Pause straight away: after Resume, the words spoken after Pause are
  not sent (v1.1.3).

### W4. Push-to-talk, native sender
- [ ] The morning check in `docs/windows-push-to-talk/README.md`, steps 1–10. Real keys
  have never been tested.
- [ ] A quick tap right after a sender restart is still a tap, not a hold (v1.1.3). Restart
  `ptt` (off, then on), then tap once while it speaks.
- [ ] In the Copilot app: the listener started with `-FocusPattern '^github$'` fires while
  the app is in front.

### W5. Voice queue with two sessions
- [ ] Two Claude Code sessions in native Windows terminals, both in voice: they take turns.
  `voicemode conch status` shows the holder and the waiter.
- [ ] A session holding the mic with `hold_conch` hands it over after 3 replied turns while
  the other waits (v1.1.1).
- [ ] Session wake through the named pipe: queue a callback while busy, go idle, and check
  whether it is woken. **UNVERIFIED on Windows.** Record what happens.

### W6. Security (v1.1.3)
- [ ] `update_config BASH_ENV ...` is refused, and `voicemode.env` stays unchanged.
- [ ] Put `VOICEMODE_PILL_PYTHON=C:\Windows\System32\calc.exe` in `voicemode.env` and reload
  the config: the pill still uses the server's own python, and no calculator opens. Remove
  the line afterwards.

---

## Windows host + WSL (relay mode)

### R1. Push-to-talk through the relay (token is new in v1.1.3)
- [ ] In WSL, start the relay once:
  `VOICEMODE_PTT_HOST=0.0.0.0 ~/git/voicemode-local/.venv/bin/python -m voice_mode.ptt_relay`.
  `~/.voicemode/ptt-relay.token` then exists, with mode 600.
- [ ] On Windows, start `ptt-listener-windows.ps1` with no `-TokenPath`. It finds the WSL home
  by itself and logs "relay reachable".
- [ ] Hold, talk, release: answered. A tap: stop.
- [ ] Cold boot (`wsl --shutdown`, then start the listener first, before WSL is up): within
  about 30 s of WSL coming up, push-to-talk works without restarting the listener (v1.1.3).
  This ends every WSL session, so do it at a quiet moment.
- [ ] A client without the token is refused: `relay <- missing or wrong token` appears in the
  relay's output.

---

## macOS

### M1. Install
- [ ] `install-mac.sh` (with `--barge` if you want barge-in) on a Mac without a previous
  install finishes, and `voicemode-switch test-tts` speaks.
- [ ] Reconnect with `/mcp`: `voicemode-mcp` starts the push-to-talk listener once
  (`~/.voicemode/run/ptt-listener.pid` names a live process).

### M2. Pill
- [ ] The pill checklist in `docs/voice-pill/README.md`, section "Checklist for FELIX-MAC".
- [ ] With `VOICEMODE_PILL=true` **only in the environment** (not in voicemode.env), the
  window comes up and stays up (v1.1.3; it used to exit on every FELIX launch).
- [ ] The Control+Option+Space keys show while the listener runs, and disappear with
  `VOICEMODE_PTT_ENABLED=false` (v1.1.3).
- [ ] Pause, then make the backend hang (or wait for a watchdog restart): after the restart
  the pill is still paused (v1.1.3).

### M3. Barge-in
- [ ] Headphones: talking over a reply stops it. The first 0.8–0.9 s of each reply is deaf
  (known, n=6).
- [ ] Built-in speakers: does macOS voice processing (echo cancellation) keep the reply's own
  voice from triggering barge? Only tested with fakes so far.

### M4. Push-to-talk
- [ ] Hold Control+Option+Space while it speaks: the reply stops, you talk, release: answered.
- [ ] Quit the listener (Ctrl+C) in the middle of a hold: the recording ends; it is not left
  open for 120 s (v1.1.3).

---

## After the runs

- Record each FAIL here and fix it in v1.1.4.
- Copy the results into the matching doc's "Not tested" section, so those sections stay true:
  `docs/voice-pill/README.md`, `docs/voice-barge-in/README.md`,
  `docs/windows-push-to-talk/README.md`, `docs/windows-native/README.md`.
