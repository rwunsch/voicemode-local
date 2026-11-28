# Audio recover: a mic stream that fails to start

## What was found (2026-11-18)

`~/voicemode_debug.log`, 22:24:53 CET:

```
Recording with VAD failed: Error starting stream: Wait timed out [PaErrorCode -9987]
Falling back to fixed duration recording
Recording audio for 120.0s...
```

The turn was cancelled with ESC at 22:26:35; `Recorded 2880000 samples` (120 s at 24 kHz)
followed at 22:27:13. The same sequence is in the log once before, 2026-11-13 18:49:55
(found by `grep -n "PaErrorCode -9987" ~/voicemode_debug.log`, 2026-11-18: 8 lines,
2 events, each logged 4 times because two log handlers write the same line).

Upstream voice-mode 8.12.0, `record_audio_with_silence_detection` (converse.py:1619-1685):

- After the silence-detecting stream fails, PortAudio is re-initialised and the recording
  retried only for errors whose text has one of `device unavailable`, `device disconnected`,
  `invalid device`, `unanticipated host error`, `portaudio error`. "Wait timed out" has none.
- So it goes straight to `record_audio(max_duration)`: a fixed 120 s recording **without
  silence detection**. The user's turn does not end when they stop talking.
- ESC sets `stop_event`, which only the silence-detecting loop reads. The fixed recording
  ran all 120 s; only the pill's Pause ended it.

## What changed

`patches/patch_audio_recover.py`, in both manifests after `patch_vad_override.py`:

1. A start failure (`wait timed out`, `paerrorcode -9987`, `error starting stream`) that is
   not in upstream's list goes through upstream's own re-init code (`sd._terminate()`,
   `sd._initialize()`, 0.5 s) and is retried **once** with silence detection. Log line:
   `Audio start timed out - re-initialising audio and retrying once`. A thread-local marks
   the retry, so a second start failure falls back as before (`Audio start failed again
   after the retry - falling back`): one retry per recording call, never a loop.
2. Both error fallbacks ("Falling back to fixed duration recording") call
   `_vml_fallback_record`: the same `record_audio`, but a watcher thread stops that
   recording's stream within 0.1 s of `stop_event` being set. It only ever stops a stream
   started after the fallback began. Without a `stop_event` it is upstream's call exactly.
   The webrtcvad-missing and silence-detection-off paths are deliberate fixed recordings and
   are not changed.

Errors already in upstream's list (also when the text has "error starting stream", e.g.
"Error starting stream: Unanticipated host error") keep upstream's path, line for line.

## Settings

| setting | default | effect |
|---|---|---|
| `VOICEMODE_AUDIO_RECOVER` | `true` | `false`/`0`/`no`/`off`: no retry on a start failure |
| `VOICEMODE_FALLBACK_RECORD_MAX_S` | unset (= `max_duration`) | a number above 0 caps the fixed fallback's length; invalid or not smaller: ignored |

Both are read at each recording, no restart.

**behaviour change:** a mic stream that fails to start ("Wait timed out", -9987) is
re-initialised and retried once with silence detection instead of going straight to a
fixed-length recording; old behaviour via `VOICEMODE_AUDIO_RECOVER=false`.

**behaviour change:** ESC / a cancelled converse now ends a fixed-length fallback recording
within about 0.1 s instead of letting it run to `max_duration`. No switch: the old
behaviour only kept a thread recording for a caller that had already gone.

**behaviour change (Talos on FELIX #2104):** Done (the pill's Done, or any `skip_forward`)
now also ends a fixed-length fallback recording within about 0.1 s, and only the audio
recorded until then is transcribed, not the rest of the `max_duration` buffer: the cut is at
the frames sounddevice wrote (`sd.rec` allocates with `np.empty`, so the rest is not audio),
or, if sounddevice's private frame count is not there, at the time since the stream was
first seen, which never counts past what was recorded.
Before, the pill showed Listening and offered Done, but nothing read it until `max_duration`.
No switch: Done that does nothing is the bug.

`VOICEMODE_FALLBACK_RECORD_MAX_S` changes nothing until set.

## Tests

`tests/test_audio_recover_patch.py` (fake sounddevice, upstream's except blocks verbatim,
in upstream's and patch_voice_pill's shapes): -9987 gives one re-init and one silence-detecting
retry; a second failure falls back; no loop; the next recording may retry again; errors
upstream handles, a non-recoverable error, and the switch off behave exactly as unpatched
(same calls, same log lines); the stop flag ends the fallback in both fallback paths; an
earlier stream is never stopped; the cap; drift, idempotence, CRLF; pristine 8.12.0 and
the whole manifest (default and native Windows), with `VM812_SRC`.

## Not tested

- **A real WSLg stall.** -9987 cannot be produced on demand; the retry is tested against a
  fake that raises the exact message. Whether a re-init actually clears a WSLg "Wait timed
  out" is unknown until it happens again with this patch in place (look for the retry line
  in the log, then whether the retry recorded).
- **Stopping a real PortAudio stream from the watcher thread.** The fake's `stop()` ends
  `sd.wait()`; on real PortAudio `Pa_StopStream` should call the finished callback that
  `sd.wait()` waits on, as the pill's Pause (`sd.stop()`) already relies on. Not run on a
  real device.
- `sd._terminate()` ends every PortAudio stream in the process, as upstream's existing
  re-init already does. It runs after this recording's own stream has closed; other streams
  in the same process (if any were open) are not checked.
