# Upstream issue draft — `simpleaudio` blocks every Windows install without a C++ compiler

**Target:** new issue on `mbailey/voicemode`. Related: #524 (open, native Windows install/start),
#239 (open, Windows STT analysis), #117 (closed, Ubuntu 24.04 — same dependency, different platform).
**Type:** bug report with a one-line fix.
**Submit as:** `gh auth switch --user rwunsch` first.
**Status:** draft, NOT submitted — awaiting Robert's go-ahead.

**Confidence: high.** The mechanism is proven by a resolver run rather than inferred, the dependency
has broken installs on at least three platforms already, and the fix is deleting one line.

---

## Draft issue

**Title:** `simpleaudio` is a hard dependency that nothing imports, and it blocks Windows installs

### What happens

On native Windows, `uv tool install voice-mode` fails unless the machine has Visual Studio Build
Tools installed, because `simpleaudio` must be compiled from source.

`simpleaudio`'s newest Windows wheels are for **Python 3.7 and 3.8** (version 1.0.4, released 2020).
voice-mode requires Python `>=3.10`. So there is no wheel for any Python voice-mode supports, on any
platform where one isn't built locally.

### Proof that it is the only blocker

Requiring wheels for every C-extension dependency, and allowing source builds for everything else:

```
uv pip install --only-binary=simpleaudio --only-binary=webrtcvad-wheels \
               --only-binary=numpy --only-binary=scipy --only-binary=sounddevice voice-mode
```

```
  × No solution found when resolving dependencies:
  ╰─▶ ... depend on simpleaudio, we can conclude that ... cannot be used.
hint: Wheels are required for `simpleaudio` because building from source is disabled
      for `simpleaudio`
```

`numpy`, `scipy`, `sounddevice` and `webrtcvad-wheels` all resolve from wheels. **Only `simpleaudio`
fails.** Remove it and the install is compiler-free on Windows.

### Nothing imports it

In the whole of voice_mode, `simpleaudio` appears once — in a comment:

```python
# voice_mode/core.py:579
# Try using PyDub's playback (requires simpleaudio or pyaudio)
try:
    from pydub.playback import play as pydub_play
```

That is the **third** playback choice. `sounddevice` does the real work (imported in 11 files). The
`pydub` call is only reached after sounddevice raises, is wrapped in `try`/`except Exception`, and
has a further fallback after it that writes the audio to the user's home directory.

And `pydub.playback.play()` itself degrades: it tries `simpleaudio`, then `pyaudio`, then
**`ffplay`** — and ffmpeg is already a documented system requirement for voice-mode. So the fallback
path keeps working without `simpleaudio` installed.

### This has bitten people before

- **#117** (Ubuntu 24.04, Nov 2025) — the reporter's output names `simpleaudio (v1.0.4) was ... for
  version 3.13 and 3.8` as part of the resolution failure.
- **#13** and **#319** both touch build failures involving it on Nix/WSL.

Each was handled as a platform problem. The common factor is a dependency with no modern wheels.

### Suggested fix

Drop `simpleaudio` from `dependencies` in `pyproject.toml`. If the pydub fallback should keep a
compiled backend where one is available, an optional extra (`pip install voice-mode[pydub-audio]`)
expresses that without making every install need a compiler.

Happy to open the PR if that is the direction you would take. It is a one-line change plus a note in
the install docs.

### Environment

Windows 11, native (not WSL). Python 3.12.10, uv 0.12.3, voice-mode 8.12.0. The install *succeeded*
on this machine only because it happens to have Visual Studio Build Tools 2022 — which is how the
cause was found: the question was not "did it install" but "why did it install here".

For completeness, everything else on native Windows worked: `voice-mode --version` runs,
`sounddevice` enumerates 31 input and 36 output devices with correct defaults, and `kokoro-onnx`
installs from wheels alone.

---

## Notes for us, not for upstream

If upstream declines or goes quiet, FELIX can carry a `uv` override that drops `simpleaudio` at
install time, which keeps us on stock upstream otherwise. Building and shipping our own
`simpleaudio` wheel is possible and is the wrong answer — it would make us the maintainer of a
binary wheel for an unmaintained 2020 package. See
`felix-framework` PR #621 for where this sits in the FELIX decision.
