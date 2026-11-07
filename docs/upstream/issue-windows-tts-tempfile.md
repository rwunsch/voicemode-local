# FILED 2026-11-07 (#586 + PR #587) — issue + small PR

**Title:** Windows: every buffered TTS turn fails with WinError 32 — the temp file is deleted while still open (TTS side of #135)

**Body:**

On native Windows, buffered TTS (`VOICEMODE_STREAMING_ENABLED=false`, or any path that
writes a temp file) fails after the audio has already played:

```
TTS service connection failed: [WinError 32] The process cannot access the file because it
is being used by another process: '...\tmpXXXX.pcm'
```

The turn then never starts listening.

**Cause.** `core.py` writes the reply to `tempfile.NamedTemporaryFile(..., delete=False)` and
calls `os.unlink(tmp_file.name)` *inside* the `with` block that still holds the file open.
Linux and macOS allow that; Windows refuses. The exception escapes `text_to_speech`, and the
failover reports it as a connection failure.

In v9.0.2:
- `voice_mode/core.py`: the `with` blocks at lines 447 and 770; `os.unlink(tmp_file.name)` at
  lines 558, 580, 592, 597, 614, 622.
- `voice_mode/streaming.py`: `os.unlink(tmp_wav.name)` at lines 650 and 920 (save-a-copy path).

The STT side had the same problem and was fixed by closing the file first (#135). The TTS side
never got that fix.

**Seen:** Windows 11 25H2, Python 3.12, voice-mode 8.12.0, native (no WSL): 3 of 3 turns failed
this way in one run. After closing the handle before deleting, every turn played and listened
(n=3 turns). Line numbers above are from reading v9.0.2; not re-run on 9.0.2.

**Fix (proposed PR, one change):** close the handle, then delete, and never let a failed delete
fail the turn, e.g.

```python
def _close_unlink(handle):
    try:
        handle.close()
        os.unlink(handle.name)
    except OSError:
        pass
```

used at the six `core.py` sites, and `tmp_wav.close()` before the two `streaming.py` unlinks.
