# Push-to-Talk Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in push-to-talk mode to `converse()` — Control-Space toggles listening (short press = normal silence-detection listen, long press/hold = raw hold-to-talk, second short press manually stops early), interrupts TTS playback on press (barge-in), scoped to when the Claude Code terminal window has focus, working on native Linux first and WSL2 second.

**Architecture:** A cross-platform pure state machine (`ptt_core.py`) classifies raw key events into `press` / `hold_start` / `short_press` / `hold_release`. A small TCP relay bus (`ptt_ipc.py`, localhost-only) lets a platform-specific key-listener process (producer) and `converse()` (consumer) talk without sharing process memory — the same shape on every platform, including WSL2 where the producer runs on the Windows host and reaches the Linux guest via the `.wslconfig` `localhostForwarding` this machine already has enabled. `converse.py` and `core.py` get small, pattern-anchored patches (same style as the existing session-queue patch) rather than full file copies.

**Tech Stack:** Python 3.10+, stdlib `asyncio`/`socket`/`threading` for the IPC bus, `pynput` (new dependency, Linux/X11 backend) + `xdotool` (system package) for the native Linux listener, `psutil` (already a voice-mode dependency) for process-tree walking.

## Global Constraints

- Default is **off**: `VOICEMODE_PTT_ENABLED=false`. When off, every patched code path must be byte-for-byte behaviorally identical to today (verified by the existing `test_no_speech_timeout.py`-style tests continuing to pass unmodified).
- Hotkey: **Control-Space**. Hold threshold: **1.0s**, exposed as `VOICEMODE_PTT_HOLD_THRESHOLD` (float seconds).
- Toggling happens through the existing upstream `update_config`/`config_reload` MCP tools (writes `~/.voicemode/voicemode.env`) — this plan does **not** add a new CLI switch for turning PTT on/off.
- Focus scope: the hotkey only acts while the specific Claude Code terminal window has focus — never a true system-wide hook. v1 known limitation (from the design doc): two sessions with identical terminal titles in the same directory are not disambiguated.
- All new patches follow the existing pattern-anchored style in `patches/` (see `patches/patch_converse_queue.py`): exact-string anchors validated with an expected occurrence count; the patcher exits 1 (loudly) rather than silently half-patching if upstream has drifted; already-patched files are a no-op.
- Design reference: `docs/superpowers/specs/2026-07-07-push-to-talk-design.md`.

---

## File Structure

| File | Responsibility |
|---|---|
| `patches/ptt_core.py` (new) | Pure press/hold/release classification state machine. No I/O. Installed to `voice_mode/ptt_core.py`. |
| `patches/ptt_ipc.py` (new) | TCP relay event bus (server + client helpers). No platform-specific code. Installed to `voice_mode/ptt_ipc.py`. |
| `patches/ptt_bridge.py` (new) | `converse()`-side glue: env config, `wait_for_trigger()`, `watch_for_stop()`. Installed to `voice_mode/ptt_bridge.py`. |
| `patches/ptt_playback_bridge.py` (new) | Registry for the live `NonBlockingAudioPlayer`, thread-based barge-in watcher. Installed to `voice_mode/ptt_playback_bridge.py`. |
| `patches/patch_converse_ptt.py` (new) | Surgical patcher: `record_audio_with_silence_detection` gets `stop_event`/`ptt_hold_mode` params + loop hook; `converse()` gets the pre-recording trigger wait and the TTS-await barge-in race. |
| `patches/patch_core_ptt.py` (new) | Surgical patcher: `core.py`'s `text_to_speech()` registers/clears the live player with `ptt_playback_bridge`. |
| `patches/ptt_listener_linux.py` (new) | Native Linux (X11) key listener: `pynput` capture + `xdotool`-based focus filter + `ptt_core` classification, sends events to `ptt_ipc`. |
| `patches/ptt_listener_windows.py` (new) | WSL2 companion, run on the Windows host: same shape as the Linux listener, `pywin32` for key capture/focus, connects out to the WSL guest's relay port. |
| `patches/apply.sh` (modify) | Copy the new modules, run the two new patchers, conditionally spawn the Linux listener when `VOICEMODE_PTT_ENABLED=true`. |
| `voicemode-switch` (modify) | Add read-only `ptt status` subcommand (mirrors the existing `queue` subcommand). |
| `tests/test_ptt_core.py`, `tests/test_ptt_ipc.py`, `tests/test_ptt_recording.py`, `tests/test_converse_ptt_patch.py`, `tests/test_core_ptt_patch.py`, `tests/test_ptt_listener_linux.py` (new) | Unit + integration tests per module, following the existing `tests/test_voice_queue.py` / `tests/test_converse_queue_patch.py` / `tests/test_no_speech_timeout.py` conventions. |

---

### Task 1: `ptt_core.py` — press/hold/release state machine

**Files:**
- Create: `patches/ptt_core.py`
- Test: `tests/test_ptt_core.py`

**Interfaces:**
- Produces: `PTTAction` enum (`PRESS`, `SHORT_PRESS`, `HOLD_START`, `HOLD_RELEASE`); `PTTKeyState` class with `on_press(ts: float) -> PTTAction`, `poll_hold(now: float) -> Optional[PTTAction]`, `on_release(ts: float) -> Optional[PTTAction]`; `DEFAULT_HOLD_THRESHOLD = 1.0`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ptt_core.py
"""Tests for patches/ptt_core.py — press/hold/release classification."""
import sys
from pathlib import Path

PATCHES_DIR = Path(__file__).parent.parent / "patches"
sys.path.insert(0, str(PATCHES_DIR))

import ptt_core  # noqa: E402


def test_press_always_returns_press_action():
    state = ptt_core.PTTKeyState(hold_threshold=1.0)
    assert state.on_press(0.0) == ptt_core.PTTAction.PRESS


def test_key_repeat_press_is_idempotent():
    state = ptt_core.PTTKeyState(hold_threshold=1.0)
    state.on_press(0.0)
    # OS key-repeat sends more press events while held; must not reset the clock
    state.on_press(0.5)
    action = state.on_release(0.8)
    assert action == ptt_core.PTTAction.SHORT_PRESS


def test_short_press_below_threshold():
    state = ptt_core.PTTKeyState(hold_threshold=1.0)
    state.on_press(0.0)
    assert state.poll_hold(0.5) is None
    assert state.on_release(0.9) == ptt_core.PTTAction.SHORT_PRESS


def test_hold_crossing_threshold_fires_once():
    state = ptt_core.PTTKeyState(hold_threshold=1.0)
    state.on_press(0.0)
    assert state.poll_hold(0.5) is None
    assert state.poll_hold(1.0) == ptt_core.PTTAction.HOLD_START
    # Continuing to poll while still held must not fire again
    assert state.poll_hold(1.5) is None


def test_release_after_hold_returns_hold_release():
    state = ptt_core.PTTKeyState(hold_threshold=1.0)
    state.on_press(0.0)
    state.poll_hold(1.0)
    assert state.on_release(1.6) == ptt_core.PTTAction.HOLD_RELEASE


def test_spurious_release_returns_none():
    state = ptt_core.PTTKeyState(hold_threshold=1.0)
    assert state.on_release(5.0) is None


def test_state_resets_after_release_for_next_cycle():
    state = ptt_core.PTTKeyState(hold_threshold=1.0)
    state.on_press(0.0)
    state.on_release(0.3)
    state.on_press(1.0)
    assert state.on_release(1.2) == ptt_core.PTTAction.SHORT_PRESS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/wunsch/git/voicemode-local && python3 -m pytest tests/test_ptt_core.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ptt_core'`

- [ ] **Step 3: Write the implementation**

```python
# patches/ptt_core.py
"""Pure press/hold/release classification for push-to-talk.

No I/O, no threading, no wall-clock reads — a state machine driven entirely
by timestamps the caller supplies. Used identically by every platform-
specific key listener (native Linux, the WSL2 Windows companion, future
macOS/Windows-native listeners) so the interaction semantics — short press
vs. hold, the threshold, key-repeat handling — live in exactly one place.

Design: docs/superpowers/specs/2026-07-07-push-to-talk-design.md
Installed into voice_mode/ptt_core.py by patches/apply.sh.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

DEFAULT_HOLD_THRESHOLD = 1.0  # seconds; VOICEMODE_PTT_HOLD_THRESHOLD


class PTTAction(Enum):
    PRESS = "press"                # key went down (fires immediately, every press)
    SHORT_PRESS = "short_press"    # released before the hold threshold
    HOLD_START = "hold_start"      # held past the threshold, still down
    HOLD_RELEASE = "hold_release"  # release of a press that had reached HOLD_START


@dataclass
class PTTKeyState:
    """Tracks one key-down/key-up cycle for a single hotkey."""
    hold_threshold: float = DEFAULT_HOLD_THRESHOLD
    _press_ts: Optional[float] = field(default=None, init=False)
    _hold_fired: bool = field(default=False, init=False)

    def on_press(self, ts: float) -> PTTAction:
        """Call when the hotkey goes down. OS key-repeat sends this
        repeatedly while held — only the first call starts the clock."""
        if self._press_ts is None:
            self._press_ts = ts
            self._hold_fired = False
        return PTTAction.PRESS

    def poll_hold(self, now: float) -> Optional[PTTAction]:
        """Call periodically while the key is down (e.g. every 50ms).
        Returns HOLD_START exactly once, the moment the threshold is
        crossed; None at every other call, including while still held
        after HOLD_START has already fired."""
        if self._press_ts is None or self._hold_fired:
            return None
        if now - self._press_ts >= self.hold_threshold:
            self._hold_fired = True
            return PTTAction.HOLD_START
        return None

    def on_release(self, ts: float) -> Optional[PTTAction]:
        """Call when the hotkey goes up. Returns the resulting action, or
        None for a spurious release with no matching press."""
        if self._press_ts is None:
            return None
        was_hold = self._hold_fired
        self._press_ts = None
        self._hold_fired = False
        return PTTAction.HOLD_RELEASE if was_hold else PTTAction.SHORT_PRESS
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ptt_core.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add patches/ptt_core.py tests/test_ptt_core.py
git commit -m "feat(ptt): press/hold/release classification state machine"
```

---

### Task 2: `ptt_ipc.py` — TCP relay event bus

**Files:**
- Create: `patches/ptt_ipc.py`
- Test: `tests/test_ptt_ipc.py`

**Interfaces:**
- Consumes: nothing from Task 1 (transports opaque `PTTEvent` values; the `type` field happens to be a `PTTAction.value` string but this module doesn't import `ptt_core`).
- Produces: `PTTEvent` dataclass (`type: str`, `ts: float`); `DEFAULT_PORT = 8765`; `PTTEventServer` class (`async start()`, `async stop()`); `async def send_event(event: PTTEvent, port: int = DEFAULT_PORT) -> None` (one-shot producer client); `async def read_events(port: int = DEFAULT_PORT) -> AsyncIterator[PTTEvent]` (consumer client, an async generator).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ptt_ipc.py
"""Tests for patches/ptt_ipc.py — the local TCP relay event bus."""
import asyncio
import sys
from pathlib import Path

import pytest

PATCHES_DIR = Path(__file__).parent.parent / "patches"
sys.path.insert(0, str(PATCHES_DIR))

import ptt_ipc  # noqa: E402


@pytest.fixture
async def server():
    srv = ptt_ipc.PTTEventServer(port=0)  # port=0: OS picks a free port
    await srv.start()
    yield srv
    await srv.stop()


@pytest.mark.asyncio
async def test_single_producer_single_consumer(server):
    port = server.actual_port
    events = []

    async def consume():
        async for event in ptt_ipc.read_events(port=port):
            events.append(event)
            if len(events) == 2:
                break

    consumer_task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)  # let the consumer connect before sending

    await ptt_ipc.send_event(ptt_ipc.PTTEvent(type="press", ts=1.0), port=port)
    await ptt_ipc.send_event(ptt_ipc.PTTEvent(type="short_press", ts=1.3), port=port)

    await asyncio.wait_for(consumer_task, timeout=2.0)
    assert events == [
        ptt_ipc.PTTEvent(type="press", ts=1.0),
        ptt_ipc.PTTEvent(type="short_press", ts=1.3),
    ]


@pytest.mark.asyncio
async def test_event_relayed_to_multiple_consumers(server):
    port = server.actual_port
    received_a, received_b = [], []

    async def consume(sink):
        async for event in ptt_ipc.read_events(port=port):
            sink.append(event)
            break

    task_a = asyncio.create_task(consume(received_a))
    task_b = asyncio.create_task(consume(received_b))
    await asyncio.sleep(0.05)

    await ptt_ipc.send_event(ptt_ipc.PTTEvent(type="hold_start", ts=2.0), port=port)

    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=2.0)
    assert received_a == [ptt_ipc.PTTEvent(type="hold_start", ts=2.0)]
    assert received_b == [ptt_ipc.PTTEvent(type="hold_start", ts=2.0)]


@pytest.mark.asyncio
async def test_read_events_raises_when_no_server_running():
    with pytest.raises((ConnectionRefusedError, OSError)):
        async for _ in ptt_ipc.read_events(port=1):  # privileged/unbound port
            pass
```

Add `pytest-asyncio` to the dev dependencies if not already present:

Run: `grep -r pytest-asyncio pyproject.toml requirements*.txt 2>/dev/null || echo "MISSING"`
If missing, install it: `uv pip install pytest-asyncio` (or `pip install pytest-asyncio`) and add `asyncio_mode = "auto"` under `[tool.pytest.ini_options]` in `pyproject.toml` if that section doesn't already set it — check first with `grep -n asyncio_mode pyproject.toml`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ptt_ipc.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ptt_ipc'`

- [ ] **Step 3: Write the implementation**

```python
# patches/ptt_ipc.py
"""Local TCP relay bus for push-to-talk key events.

Any connected client can be a producer (a platform key listener sending
classified events), a consumer (converse.py reading them), or both — the
server just relays every line it receives to every OTHER connected client.
This one relay shape covers every deployment:

- Native Linux/macOS/Windows: the listener process and converse() are two
  local TCP clients of the same local server.
- WSL2: the listener runs on the Windows host and connects to
  127.0.0.1:<port>, which WSL2's `localhostForwarding` (already enabled in
  this project's .wslconfig) delivers into the Linux guest where the relay
  server and converse() both live.

The relay is intentionally dumb: it does not parse or validate — client
_send_event/read_events already produce/consume well-formed JSON lines. A
malformed line from a misbehaving client is simply relayed as-is; readers
handle a JSONDecodeError by ending their iteration (see read_events).

Known v1 limitation: one shared port per machine — concurrent PTT-enabled
sessions all see every event (docs/superpowers/specs/2026-07-07-push-to-talk-design.md).

Installed into voice_mode/ptt_ipc.py by patches/apply.sh.
"""
import asyncio
import json
from dataclasses import asdict, dataclass
from typing import AsyncIterator, Optional, Set

DEFAULT_PORT = 8765


@dataclass(frozen=True)
class PTTEvent:
    type: str  # "press" | "hold_start" | "short_press" | "hold_release"
    ts: float


class PTTEventServer:
    """One instance per machine. Started by whichever process owns the
    platform key listener (see patches/ptt_listener_linux.py)."""

    def __init__(self, port: int = DEFAULT_PORT):
        self._requested_port = port
        self._server: Optional[asyncio.AbstractServer] = None
        self._writers: Set[asyncio.StreamWriter] = set()

    @property
    def actual_port(self) -> int:
        """The bound port — resolves port=0 to whatever the OS picked."""
        assert self._server is not None, "start() not called"
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_client, "127.0.0.1", self._requested_port
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for w in list(self._writers):
            w.close()
        self._writers.clear()

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writers.add(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                for w in list(self._writers):
                    if w is writer:
                        continue
                    try:
                        w.write(line)
                        await w.drain()
                    except (ConnectionError, OSError):
                        self._writers.discard(w)
        finally:
            self._writers.discard(writer)
            writer.close()


async def send_event(event: PTTEvent, port: int = DEFAULT_PORT) -> None:
    """One-shot producer client: connect, send one event, disconnect."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write((json.dumps(asdict(event)) + "\n").encode())
        await writer.drain()
    finally:
        writer.close()


async def read_events(port: int = DEFAULT_PORT) -> AsyncIterator[PTTEvent]:
    """Consumer client used by converse.py. Yields events until the caller
    stops iterating (e.g. `break`) or the server connection drops. Raises
    ConnectionRefusedError/OSError immediately if no relay server is
    running — callers must treat that as "PTT unavailable right now"."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                data = json.loads(line)
                yield PTTEvent(type=data["type"], ts=data["ts"])
            except (json.JSONDecodeError, KeyError):
                break
    finally:
        writer.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ptt_ipc.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add patches/ptt_ipc.py tests/test_ptt_ipc.py
git commit -m "feat(ptt): local TCP relay bus for key events"
```

---

### Task 3: `converse.py` recording integration — hold-to-talk, short-press listen, manual early stop

**Files:**
- Create: `patches/ptt_bridge.py`
- Create: `patches/patch_converse_ptt.py`
- Test: `tests/test_ptt_recording.py` (behavior, against the real installed+patched module)
- Test: `tests/test_converse_ptt_patch.py` (patcher anchor/idempotency, mirrors `tests/test_converse_queue_patch.py`)

**Interfaces:**
- Consumes: `ptt_core.PTTKeyState`/`PTTAction` (Task 1, imported by the listener — NOT by `ptt_bridge.py`, which only reads already-classified event *types* off the wire), `ptt_ipc.read_events`/`PTTEvent` (Task 2).
- Produces: `ptt_bridge.PTT_ENABLED: bool`, `ptt_bridge.PTT_PORT: int`; `async def wait_for_trigger() -> Optional[str]` (returns `"hold"`, `"short"`, or `None` if PTT is off or the relay is unreachable); `async def watch_for_stop(mode: str) -> Tuple[threading.Event, asyncio.Task]`.
- `record_audio_with_silence_detection` gains two trailing parameters: `stop_event: Optional[threading.Event] = None`, `ptt_hold_mode: bool = False`.

Verified anchors below are quoted **exactly** from the currently-installed, already-queue-patched
`.venv/lib/python3.13/site-packages/voice_mode/tools/converse.py` (voice-mode 8.7.1 + this repo's
own queue patch). If `patches/apply.sh` has not yet installed the queue patch when this task runs,
run `./patches/apply.sh` first so these anchors exist.

- [ ] **Step 1: Write `ptt_bridge.py`**

```python
# patches/ptt_bridge.py
"""converse()-side glue between the PTT event relay bus and the recording
loop: env config, and the two entry points converse.py calls.

Installed into voice_mode/ptt_bridge.py by patches/apply.sh.
"""
import asyncio
import os
import threading
from typing import Optional, Tuple

from voice_mode import ptt_ipc

PTT_ENABLED = os.getenv("VOICEMODE_PTT_ENABLED", "false").lower() in ("true", "1", "yes", "on")
PTT_PORT = int(os.getenv("VOICEMODE_PTT_PORT", str(ptt_ipc.DEFAULT_PORT)))


async def wait_for_trigger() -> Optional[str]:
    """Block until the relay reports a classified press cycle. Returns
    "hold" or "short", or None if PTT is disabled or the relay is
    unreachable (no listener process running) — callers fall back to
    normal silence-detection listening in that case."""
    if not PTT_ENABLED:
        return None
    try:
        async for event in ptt_ipc.read_events(port=PTT_PORT):
            if event.type == "hold_start":
                return "hold"
            if event.type == "short_press":
                return "short"
    except (ConnectionRefusedError, OSError):
        return None
    return None


async def watch_for_stop(mode: str) -> Tuple[threading.Event, "asyncio.Task"]:
    """Starts a background task watching for the event that should end the
    CURRENT recording: HOLD_RELEASE for hold mode, or the next SHORT_PRESS
    for short mode (manual early stop). Returns (stop_event, task) — the
    caller passes stop_event to record_audio_with_silence_detection and
    MUST cancel the task once recording finishes, however it finished."""
    stop_event = threading.Event()

    async def _watch():
        try:
            async for event in ptt_ipc.read_events(port=PTT_PORT):
                if mode == "hold" and event.type == "hold_release":
                    stop_event.set()
                    return
                if mode == "short" and event.type == "short_press":
                    stop_event.set()
                    return
        except (ConnectionRefusedError, OSError):
            return

    task = asyncio.ensure_future(_watch())
    return stop_event, task
```

- [ ] **Step 2: Write the patcher**

```python
# patches/patch_converse_ptt.py
#!/usr/bin/env python3
"""Surgically patch voice_mode/tools/converse.py for push-to-talk.

Usage: patch_converse_ptt.py <path-to-converse.py>

Adds stop_event/ptt_hold_mode support to record_audio_with_silence_detection
and wires converse()'s primary listen call site to wait for a PTT trigger
before recording when VOICEMODE_PTT_ENABLED=true. Each anchor must occur
EXACTLY once (else exit 1 naming the anchor). Running on an already-patched
file is a no-op (exit 0).

Anchors verified against voice-mode 8.7.1 + this repo's queue patch
(patch_converse_queue.py must run first — apply.sh already orders it that
way).
"""
import sys
from pathlib import Path

MARKER = "voicemode-local push-to-talk"

# ---- anchor: record_audio_with_silence_detection signature ----
A_SIG = (
    "def record_audio_with_silence_detection(max_duration: float, "
    "disable_silence_detection: bool = False, min_duration: float = 0.0, "
    "vad_aggressiveness: Optional[int] = None, "
    "no_speech_timeout: Optional[float] = None) -> Tuple[np.ndarray, bool]:"
)
R_SIG = (
    "def record_audio_with_silence_detection(max_duration: float, "
    "disable_silence_detection: bool = False, min_duration: float = 0.0, "
    "vad_aggressiveness: Optional[int] = None, "
    "no_speech_timeout: Optional[float] = None, "
    "stop_event: Optional[\"threading.Event\"] = None, "
    "ptt_hold_mode: bool = False) -> Tuple[np.ndarray, bool]:"
)

# ---- anchor: docstring (after the no_speech_timeout doc line) ----
A_DOC = (
    "        no_speech_timeout: If set, stop after this many seconds ONLY if\n"
    "            speech never started (voicemode-local session queue contention);\n"
    "            never truncates active speech\n"
)
R_DOC = (
    "        no_speech_timeout: If set, stop after this many seconds ONLY if\n"
    "            speech never started (voicemode-local session queue contention);\n"
    "            never truncates active speech\n"
    "        stop_event: voicemode-local push-to-talk — if set, ends the\n"
    "            recording immediately, bypassing silence detection entirely\n"
    "        ptt_hold_mode: voicemode-local push-to-talk — if True, disables\n"
    "            the natural no-speech and silence-threshold stop conditions;\n"
    "            only stop_event or max_duration ends the recording\n"
)

# ---- anchor: the stall-guard check inside the main while loop ----
A_LOOP = (
    "                    if time.monotonic() - _last_chunk_at >= _stall_grace:\n"
)
R_LOOP = (
    "                    # voicemode-local push-to-talk\n"
    "                    if stop_event is not None and stop_event.is_set():\n"
    "                        logger.info(\"Push-to-talk: stop requested — ending recording\")\n"
    "                        stop_recording = True\n"
    "                        break\n"
    "                    if time.monotonic() - _last_chunk_at >= _stall_grace:\n"
)

# ---- anchor: no-speech-timeout stop condition ----
A_NO_SPEECH_STOP = (
    "                            elif no_speech_timeout is not None and recording_duration >= no_speech_timeout:\n"
    "                                logger.info(f\"No speech within {no_speech_timeout:.1f}s and sessions are waiting - yielding mic\")\n"
    "                                stop_recording = True\n"
)
R_NO_SPEECH_STOP = (
    "                            elif (not ptt_hold_mode and no_speech_timeout is not None\n"
    "                                    and recording_duration >= no_speech_timeout):\n"
    "                                logger.info(f\"No speech within {no_speech_timeout:.1f}s and sessions are waiting - yielding mic\")\n"
    "                                stop_recording = True\n"
)

# ---- anchor: silence-threshold stop condition ----
A_SILENCE_STOP = (
    "                                if recording_duration >= effective_min_duration and silence_duration_ms >= SILENCE_THRESHOLD_MS:\n"
)
R_SILENCE_STOP = (
    "                                if (not ptt_hold_mode and recording_duration >= effective_min_duration\n"
    "                                        and silence_duration_ms >= SILENCE_THRESHOLD_MS):\n"
)

# ---- anchor: primary recording call site (unique — only occurrence with the preceding debug log) ----
A_CALL = (
    "                record_start = time.perf_counter()\n"
    "                logger.debug(f\"About to call record_audio_with_silence_detection with duration={listen_duration_max}, disable_silence_detection={disable_silence_detection}, min_duration={listen_duration_min}, vad_aggressiveness={vad_aggressiveness}\")\n"
    "                audio_data, speech_detected = await asyncio.get_event_loop().run_in_executor(\n"
    "                    None, record_audio_with_silence_detection, listen_duration_max, disable_silence_detection, listen_duration_min, vad_aggressiveness, _queue_no_speech_timeout\n"
    "                )\n"
)
R_CALL = (
    "                # voicemode-local push-to-talk: block for a classified\n"
    "                # press before recording (no-op when PTT is disabled or\n"
    "                # unreachable — wait_for_trigger() returns None instantly)\n"
    "                from voice_mode import ptt_bridge\n"
    "                _ptt_mode = await ptt_bridge.wait_for_trigger()\n"
    "                _ptt_stop_event, _ptt_stop_task = (\n"
    "                    await ptt_bridge.watch_for_stop(_ptt_mode) if _ptt_mode else (None, None)\n"
    "                )\n"
    "                _ptt_disable_silence = disable_silence_detection or (_ptt_mode == \"hold\")\n"
    "                record_start = time.perf_counter()\n"
    "                logger.debug(f\"About to call record_audio_with_silence_detection with duration={listen_duration_max}, disable_silence_detection={disable_silence_detection}, min_duration={listen_duration_min}, vad_aggressiveness={vad_aggressiveness}\")\n"
    "                try:\n"
    "                    audio_data, speech_detected = await asyncio.get_event_loop().run_in_executor(\n"
    "                        None, record_audio_with_silence_detection, listen_duration_max, _ptt_disable_silence, listen_duration_min, vad_aggressiveness, _queue_no_speech_timeout, _ptt_stop_event, _ptt_mode == \"hold\"\n"
    "                    )\n"
    "                finally:\n"
    "                    if _ptt_stop_task is not None and not _ptt_stop_task.done():\n"
    "                        _ptt_stop_task.cancel()\n"
)

EXACT_PATCHES = [
    ("record-signature", A_SIG, R_SIG, 1),
    ("record-docstring", A_DOC, R_DOC, 1),
    ("record-loop-stop-event", A_LOOP, R_LOOP, 1),
    ("record-no-speech-stop", A_NO_SPEECH_STOP, R_NO_SPEECH_STOP, 1),
    ("record-silence-stop", A_SILENCE_STOP, R_SILENCE_STOP, 1),
    ("converse-primary-call-site", A_CALL, R_CALL, 1),
]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    text = path.read_text()

    if MARKER in text:
        print(f"[patch_converse_ptt] {path}: already patched — skipping")
        return 0

    for name, anchor, _, expected in EXACT_PATCHES:
        count = text.count(anchor)
        if count != expected:
            print(f"[patch_converse_ptt] ERROR: anchor '{name}' matched "
                  f"{count} times (expected exactly {expected}) in {path}.\n"
                  f"Upstream voice-mode (or the queue patch) has likely "
                  f"changed — update the anchors in patches/patch_converse_ptt.py.",
                  file=sys.stderr)
            return 1

    text = f"# {MARKER}\n" + text
    for _, anchor, replacement, _expected in EXACT_PATCHES:
        text = text.replace(anchor, replacement)

    compile(text, str(path), "exec")  # syntax safety net before writing
    path.write_text(text)
    print(f"[patch_converse_ptt] patched {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Write the patcher test**

```python
# tests/test_converse_ptt_patch.py
"""Tests for patches/patch_converse_ptt.py (surgical converse.py patcher)."""
import subprocess
import sys
from glob import glob
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
QUEUE_PATCHER = REPO / "patches" / "patch_converse_queue.py"
PTT_PATCHER = REPO / "patches" / "patch_converse_ptt.py"


def _installed_converse():
    hits = glob(str(REPO / ".venv" / "lib" / "python*" /
                    "site-packages" / "voice_mode" / "tools" / "converse.py"))
    hits += glob(str(REPO / ".venv" / "Lib" / "site-packages" /
                     "voice_mode" / "tools" / "converse.py"))
    return Path(hits[0]) if hits else None


@pytest.fixture
def converse_copy(tmp_path):
    src = _installed_converse()
    if src is None:
        pytest.skip("voice-mode not installed in .venv")
    dst = tmp_path / "converse.py"
    text = src.read_text()
    if "voicemode-local session queue" not in text:
        # The PTT patch's anchors assume the queue patch already ran.
        subprocess.run([sys.executable, str(QUEUE_PATCHER), str(src)], check=False)
        text = src.read_text()
    dst.write_text(text)
    return dst


def test_patch_applies_cleanly(converse_copy):
    r = subprocess.run([sys.executable, str(PTT_PATCHER), str(converse_copy)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    text = converse_copy.read_text()
    assert "ptt_bridge" in text
    assert "stop_event: Optional[\"threading.Event\"] = None" in text
    assert "ptt_hold_mode: bool = False" in text
    compile(text, str(converse_copy), "exec")


def test_patch_is_idempotent(converse_copy):
    subprocess.run([sys.executable, str(PTT_PATCHER), str(converse_copy)], check=True)
    r = subprocess.run([sys.executable, str(PTT_PATCHER), str(converse_copy)],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert "already patched" in r.stdout
```

Run: `python3 -m pytest tests/test_converse_ptt_patch.py -v`
Expected: FAIL (`ModuleNotFoundError`/`FileNotFoundError` — the patcher file doesn't exist yet if you're executing steps out of order; if Step 2 is already done, expect PASS here and move to Step 4).

- [ ] **Step 4: Apply the patch to the local venv and write the behavior test**

Run: `python3 patches/patch_converse_ptt.py .venv/lib/python3.13/site-packages/voice_mode/tools/converse.py`
Expected: `[patch_converse_ptt] patched .../converse.py`

```python
# tests/test_ptt_recording.py
"""Behavior tests for record_audio_with_silence_detection's PTT hooks.

Same fake-sounddevice/fake-VAD harness as tests/test_no_speech_timeout.py,
run against the REAL installed (patched) voice_mode.tools.converse so this
exercises the actual runtime code path, not a reimplementation.
"""
import threading
import time
import types

import numpy as np
import pytest

converse = pytest.importorskip("voice_mode.tools.converse")

CHUNK_MS = 30


class FakeVad:
    def __init__(self, aggressiveness):
        self.calls = 0

    def is_speech(self, chunk_bytes, sample_rate):
        self.calls += 1
        return False  # silent user throughout — isolates the stop_event/hold_mode behavior


class FakeInputStream:
    def __init__(self, samplerate, channels, dtype, callback, blocksize):
        self.callback = callback
        self.blocksize = blocksize
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._produce, daemon=True)

    def _produce(self):
        chunk = np.zeros((self.blocksize, 1), dtype=np.int16)
        while not self._stop.is_set():
            self.callback(chunk, self.blocksize, None, None)
            time.sleep(0.001)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=2)
        return False


@pytest.fixture
def vad_env(monkeypatch):
    fake_sd = types.SimpleNamespace(InputStream=FakeInputStream, PortAudioError=RuntimeError)
    monkeypatch.setattr(converse, "sd", fake_sd)
    monkeypatch.setattr(converse, "webrtcvad", types.SimpleNamespace(Vad=lambda a: FakeVad(a)))
    monkeypatch.setattr(converse, "VAD_AVAILABLE", True)
    monkeypatch.setattr(converse, "DISABLE_SILENCE_DETECTION", False)
    monkeypatch.setattr(converse, "VAD_DEBUG", False)
    monkeypatch.setattr(converse, "DEBUG", False)
    monkeypatch.setattr(converse, "SILENCE_THRESHOLD_MS", 1000)
    monkeypatch.setattr(converse, "MIN_RECORDING_DURATION", 0.0)


def _recorded_seconds(audio):
    return len(audio) / converse.SAMPLE_RATE


def test_stop_event_ends_recording_immediately(vad_env):
    stop_event = threading.Event()

    def _trigger_soon():
        time.sleep(0.2)
        stop_event.set()

    threading.Thread(target=_trigger_soon, daemon=True).start()
    audio, speech_detected = converse.record_audio_with_silence_detection(
        max_duration=10.0, min_duration=0.0, vad_aggressiveness=3,
        stop_event=stop_event,
    )
    dur = _recorded_seconds(audio)
    assert dur < 1.0, f"stop_event should end recording almost immediately, got {dur:.2f}s"


def test_hold_mode_ignores_no_speech_timeout(vad_env):
    """A silent user in hold mode must NOT be auto-stopped by no_speech_timeout
    — only stop_event or max_duration may end a hold-to-talk recording."""
    audio, speech_detected = converse.record_audio_with_silence_detection(
        max_duration=0.6, min_duration=0.0, vad_aggressiveness=3,
        no_speech_timeout=0.1, ptt_hold_mode=True,
    )
    dur = _recorded_seconds(audio)
    assert dur > 0.4, f"hold mode should ignore no_speech_timeout, stopped at {dur:.2f}s"


def test_normal_mode_still_honors_no_speech_timeout(vad_env):
    """Regression: ptt_hold_mode=False (the default) must not change
    existing no_speech_timeout behavior."""
    audio, speech_detected = converse.record_audio_with_silence_detection(
        max_duration=10.0, min_duration=0.0, vad_aggressiveness=3,
        no_speech_timeout=0.2,
    )
    dur = _recorded_seconds(audio)
    assert dur < 1.0, f"no_speech_timeout should still fire, got {dur:.2f}s"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_converse_ptt_patch.py tests/test_ptt_recording.py -v`
Expected: all passed

- [ ] **Step 6: Run the full existing suite to confirm no regression**

Run: `python3 -m pytest tests/ -v`
Expected: all passed, including `tests/test_no_speech_timeout.py` and `tests/test_voice_queue.py` unchanged

- [ ] **Step 7: Commit**

```bash
git add patches/ptt_bridge.py patches/patch_converse_ptt.py tests/test_converse_ptt_patch.py tests/test_ptt_recording.py
git commit -m "feat(ptt): converse.py recording integration — hold, short-press, manual stop"
```

---

### Task 4: Barge-in — interrupt TTS playback on press

**Files:**
- Create: `patches/ptt_playback_bridge.py`
- Create: `patches/patch_core_ptt.py`
- Modify: `patches/patch_converse_ptt.py` (add one more anchor/replacement to the `EXACT_PATCHES` list from Task 3)
- Test: `tests/test_core_ptt_patch.py`

**Interfaces:**
- Consumes: `ptt_ipc.read_events`/`PTTEvent` (Task 2).
- Produces: `ptt_playback_bridge.register(player)`, `ptt_playback_bridge.clear(player)`, `ptt_playback_bridge.stop_current() -> bool`.

**Why a raw thread, not an asyncio task:** `core.py`'s real TTS playback calls `player.wait()` — a synchronous `threading.Event.wait()` — directly inside an `async def`, without an executor. That blocks the entire event loop for the duration of playback, so an `asyncio.Task`-based watcher would never get scheduled in time to react. This project already hit exactly this problem for the session-queue's audio keepalive (`core.py`'s `_vml_keepalive` — "a real OS thread (not asyncio) refreshes the queue floor while audio plays"); the barge-in watcher uses the same fix: a plain `threading.Thread` with a **blocking, synchronous** socket, which the GIL lets run concurrently with the blocked main thread.

- [ ] **Step 1: Write `ptt_playback_bridge.py`**

```python
# patches/ptt_playback_bridge.py
"""Registry for the currently-playing NonBlockingAudioPlayer, so a
push-to-talk press can interrupt TTS playback immediately (barge-in).

The watcher thread here is a plain threading.Thread with a BLOCKING
synchronous socket — not asyncio — because core.py's real playback path
blocks the event loop for the whole duration of playback (see
patches/patch_core_ptt.py for why). Only a separate OS thread can run
concurrently with that block; the GIL is released during the blocking
socket read and during player.wait()'s Event.wait().

Installed into voice_mode/ptt_playback_bridge.py by patches/apply.sh.
"""
import json
import logging
import os
import socket
import threading
from typing import Optional

logger = logging.getLogger("voicemode.ptt_playback_bridge")

_lock = threading.Lock()
_current_player = None

PTT_ENABLED = os.getenv("VOICEMODE_PTT_ENABLED", "false").lower() in ("true", "1", "yes", "on")
PTT_PORT = int(os.getenv("VOICEMODE_PTT_PORT", "8765"))


def register(player) -> None:
    global _current_player
    with _lock:
        _current_player = player


def clear(player) -> None:
    global _current_player
    with _lock:
        if _current_player is player:
            _current_player = None


def stop_current() -> bool:
    """Stop whatever is currently playing, if anything. Safe to call even
    if nothing is playing (returns False)."""
    with _lock:
        player = _current_player
    if player is None:
        return False
    player.stop()
    return True


def start_barge_in_watcher() -> "threading.Event":
    """Starts a background thread that calls stop_current() the instant a
    PTT press arrives. Returns a threading.Event the caller sets to stop
    the watcher once playback has ended normally."""
    done = threading.Event()

    def _watch():
        if not PTT_ENABLED:
            return
        try:
            sock = socket.create_connection(("127.0.0.1", PTT_PORT), timeout=2)
        except OSError:
            return  # no listener process running — barge-in unavailable
        try:
            sock.settimeout(0.2)
            buf = b""
            while not done.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "press":
                        logger.info("Push-to-talk: barge-in — stopping playback")
                        stop_current()
                        return
        finally:
            sock.close()

    thread = threading.Thread(target=_watch, daemon=True, name="ptt-barge-in")
    thread.start()
    thread._done_event = done  # stashed so the caller can signal shutdown
    return done
```

- [ ] **Step 2: Write `patch_core_ptt.py`**

```python
# patches/patch_core_ptt.py
#!/usr/bin/env python3
"""Surgically patch voice_mode/core.py to register the live audio player
for push-to-talk barge-in.

Usage: patch_core_ptt.py <path-to-core.py>

Anchors verified against voice-mode 8.7.1 + this repo's audio-keepalive
patch (patch_audio_keepalive.py must run first — apply.sh already orders
it that way; the anchor below is the keepalive's own comment, which only
appears at the one real TTS speech playback site in text_to_speech(), not
at the three short chime/system-audio sites elsewhere in core.py).
"""
import sys
from pathlib import Path

MARKER = "voicemode-local push-to-talk"

A_REGISTER = (
    "                        player = NonBlockingAudioPlayer()\n"
    "                        # voicemode-local audio keepalive: a real OS thread (not asyncio)\n"
)
R_REGISTER = (
    "                        player = NonBlockingAudioPlayer()\n"
    "                        # voicemode-local push-to-talk: register for barge-in\n"
    "                        from voice_mode import ptt_playback_bridge\n"
    "                        ptt_playback_bridge.register(player)\n"
    "                        # voicemode-local audio keepalive: a real OS thread (not asyncio)\n"
)

A_CLEAR = (
    "                        try:\n"
    "                            player.play(samples_with_buffer, audio.frame_rate, blocking=False)\n"
    "                            player.wait()\n"
    "                        finally:\n"
    "                            _vml_stop.set()\n"
)
R_CLEAR = (
    "                        try:\n"
    "                            player.play(samples_with_buffer, audio.frame_rate, blocking=False)\n"
    "                            player.wait()\n"
    "                        finally:\n"
    "                            _vml_stop.set()\n"
    "                            ptt_playback_bridge.clear(player)\n"
)

EXACT_PATCHES = [
    ("register-player", A_REGISTER, R_REGISTER, 1),
    ("clear-player", A_CLEAR, R_CLEAR, 1),
]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    text = path.read_text()

    if MARKER in text:
        print(f"[patch_core_ptt] {path}: already patched — skipping")
        return 0

    for name, anchor, _, expected in EXACT_PATCHES:
        count = text.count(anchor)
        if count != expected:
            print(f"[patch_core_ptt] ERROR: anchor '{name}' matched "
                  f"{count} times (expected exactly {expected}) in {path}.\n"
                  f"Upstream voice-mode (or the audio-keepalive patch) has "
                  f"likely changed — update patches/patch_core_ptt.py.",
                  file=sys.stderr)
            return 1

    text = f"# {MARKER}\n" + text
    for _, anchor, replacement, _expected in EXACT_PATCHES:
        text = text.replace(anchor, replacement)

    compile(text, str(path), "exec")
    path.write_text(text)
    print(f"[patch_core_ptt] patched {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Wire barge-in into `converse()`'s TTS-await, via one more anchor in `patch_converse_ptt.py`**

Add to `patches/patch_converse_ptt.py`, in the `EXACT_PATCHES` list:

```python
# ---- anchor: the TTS-with-failover await inside DJDucker ----
A_TTS_AWAIT = (
    "                    with DJDucker():\n"
    "                        tts_success, tts_metrics, tts_config = await text_to_speech_with_failover(\n"
    "                            message=message,\n"
    "                            voice=voice,\n"
    "                            model=tts_model,\n"
    "                            instructions=tts_instructions,\n"
    "                            audio_format=audio_format,\n"
    "                            initial_provider=tts_provider,\n"
    "                            speed=speed,\n"
    "                            ref_text=resolved_ref_text\n"
    "                        )\n"
)
R_TTS_AWAIT = (
    "                    with DJDucker():\n"
    "                        from voice_mode import ptt_playback_bridge\n"
    "                        _ptt_barge_in_done = ptt_playback_bridge.start_barge_in_watcher()\n"
    "                        try:\n"
    "                            tts_success, tts_metrics, tts_config = await text_to_speech_with_failover(\n"
    "                                message=message,\n"
    "                                voice=voice,\n"
    "                                model=tts_model,\n"
    "                                instructions=tts_instructions,\n"
    "                                audio_format=audio_format,\n"
    "                                initial_provider=tts_provider,\n"
    "                                speed=speed,\n"
    "                                ref_text=resolved_ref_text\n"
    "                            )\n"
    "                        finally:\n"
    "                            _ptt_barge_in_done.set()\n"
)
```

And add `("tts-barge-in-watcher", A_TTS_AWAIT, R_TTS_AWAIT, 1),` to `EXACT_PATCHES`.

Barge-in stops playback (via `player.stop()`, which unblocks `player.wait()` early with `playback_complete.set()`) but does not by itself start a new recording — `text_to_speech_with_failover` simply returns as if playback finished, and control falls through to the existing "brief pause before listening" / recording section, where Task 3's `ptt_bridge.wait_for_trigger()` runs again and picks up the in-progress or already-completed press classification. One known consequence, inherent to the classification design rather than a barge-in-specific gap: if the interrupting press turns out to be a hold, there is up to `VOICEMODE_PTT_HOLD_THRESHOLD` seconds of latency between the press and `wait_for_trigger()` seeing `hold_start`, since that event isn't emitted until the threshold is crossed.

- [ ] **Step 4: Write the patcher test**

```python
# tests/test_core_ptt_patch.py
"""Tests for patches/patch_core_ptt.py (surgical core.py patcher)."""
import subprocess
import sys
from glob import glob
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
KEEPALIVE_PATCHER = REPO / "patches" / "patch_audio_keepalive.py"
PTT_PATCHER = REPO / "patches" / "patch_core_ptt.py"


def _installed_core():
    hits = glob(str(REPO / ".venv" / "lib" / "python*" /
                    "site-packages" / "voice_mode" / "core.py"))
    hits += glob(str(REPO / ".venv" / "Lib" / "site-packages" / "voice_mode" / "core.py"))
    return Path(hits[0]) if hits else None


@pytest.fixture
def core_copy(tmp_path):
    src = _installed_core()
    if src is None:
        pytest.skip("voice-mode not installed in .venv")
    dst = tmp_path / "core.py"
    text = src.read_text()
    if "voicemode-local audio keepalive" not in text:
        subprocess.run([sys.executable, str(KEEPALIVE_PATCHER), str(src)], check=False)
        text = src.read_text()
    dst.write_text(text)
    return dst


def test_patch_applies_cleanly(core_copy):
    r = subprocess.run([sys.executable, str(PTT_PATCHER), str(core_copy)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    text = core_copy.read_text()
    assert "ptt_playback_bridge.register(player)" in text
    assert "ptt_playback_bridge.clear(player)" in text
    compile(text, str(core_copy), "exec")


def test_patch_is_idempotent(core_copy):
    subprocess.run([sys.executable, str(PTT_PATCHER), str(core_copy)], check=True)
    r = subprocess.run([sys.executable, str(PTT_PATCHER), str(core_copy)],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert "already patched" in r.stdout
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_core_ptt_patch.py -v`
Expected: 2 passed (skipped if `patch_audio_keepalive.py` isn't present yet — check `ls patches/patch_audio_keepalive.py` first; it already exists per the current repo state, so this should apply cleanly)

- [ ] **Step 6: Apply both patches to the local venv and run the full suite**

```bash
python3 patches/patch_core_ptt.py .venv/lib/python3.13/site-packages/voice_mode/core.py
python3 -m pytest tests/ -v
```
Expected: all passed

- [ ] **Step 7: Commit**

```bash
git add patches/ptt_playback_bridge.py patches/patch_core_ptt.py patches/patch_converse_ptt.py tests/test_core_ptt_patch.py
git commit -m "feat(ptt): barge-in — interrupt TTS playback on press"
```

---

### Task 5: Native Linux (X11) key listener

**Files:**
- Create: `patches/ptt_listener_linux.py`
- Test: `tests/test_ptt_listener_linux.py`

**Interfaces:**
- Consumes: `ptt_core.PTTKeyState`/`PTTAction` (Task 1), `ptt_ipc.PTTEventServer`/`send_event` (Task 2).
- Produces: `is_focused_terminal(terminal_pid: int) -> bool` (pure-ish, subprocess-backed, mockable); `resolve_terminal_pid() -> Optional[int]` (walks the process tree via `psutil`); `async def run_listener(port: int) -> None` (starts the relay server + the `pynput` capture loop; only emits/classifies events while `is_focused_terminal()` is true).

**Setup:** `pynput` is a new dependency, not yet installed.

Run: `cd /home/wunsch/git/voicemode-local && .venv/bin/pip install pynput`
Expected: `Successfully installed pynput-...`

Check `xdotool` is present (used for the X11 focus check):
Run: `which xdotool || sudo apt-get install -y xdotool`

- [ ] **Step 1: Write the failing tests for the pure/mockable pieces**

```python
# tests/test_ptt_listener_linux.py
"""Tests for patches/ptt_listener_linux.py — native Linux (X11) listener.

Focuses on the mockable pieces (focus resolution, focus check) since the
actual pynput key capture and a live X server aren't available in CI —
those are covered by the manual acceptance test in the design doc.
"""
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PATCHES_DIR = Path(__file__).parent.parent / "patches"
sys.path.insert(0, str(PATCHES_DIR))

import ptt_listener_linux  # noqa: E402


def test_is_focused_terminal_matches_pid(monkeypatch):
    fake_run = MagicMock(side_effect=[
        subprocess.CompletedProcess([], 0, stdout="0x1234\n"),
        subprocess.CompletedProcess([], 0, stdout="4321\n"),
    ])
    monkeypatch.setattr(ptt_listener_linux.subprocess, "run", fake_run)
    assert ptt_listener_linux.is_focused_terminal(terminal_pid=4321) is True


def test_is_focused_terminal_mismatch(monkeypatch):
    fake_run = MagicMock(side_effect=[
        subprocess.CompletedProcess([], 0, stdout="0x1234\n"),
        subprocess.CompletedProcess([], 0, stdout="9999\n"),
    ])
    monkeypatch.setattr(ptt_listener_linux.subprocess, "run", fake_run)
    assert ptt_listener_linux.is_focused_terminal(terminal_pid=4321) is False


def test_is_focused_terminal_xdotool_missing(monkeypatch):
    def _raise(*a, **kw):
        raise FileNotFoundError("xdotool not found")
    monkeypatch.setattr(ptt_listener_linux.subprocess, "run", _raise)
    assert ptt_listener_linux.is_focused_terminal(terminal_pid=4321) is False


def test_resolve_terminal_pid_walks_to_oldest_non_init_ancestor():
    # Build a fake process chain: terminal(100) -> shell(200) -> node(300) -> self(400)
    procs = {
        400: MagicMock(pid=400, ppid=lambda: 300),
        300: MagicMock(pid=300, ppid=lambda: 200),
        200: MagicMock(pid=200, ppid=lambda: 100),
        100: MagicMock(pid=100, ppid=lambda: 1),
    }
    for p in procs.values():
        p.parent.side_effect = lambda pid=p.pid: procs.get(procs[pid].ppid())
    with patch.object(ptt_listener_linux.psutil, "Process") as mock_process_cls:
        mock_process_cls.side_effect = lambda pid=None: procs[pid if pid else 400]
        result = ptt_listener_linux.resolve_terminal_pid(pid=400)
    assert result == 100
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ptt_listener_linux.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ptt_listener_linux'`

- [ ] **Step 3: Write the implementation**

```python
# patches/ptt_listener_linux.py
"""Native Linux (X11) push-to-talk key listener.

Captures Control-Space globally via pynput, classifies press/hold/release
with ptt_core.PTTKeyState, and only forwards classified events onto the
relay bus (ptt_ipc) while the Claude Code terminal window has focus —
checked via `xdotool`. Not usable on pure-Wayland desktops (no XWayland);
see the design doc's documented terminal-keybinding fallback for that case.

Installed into voice_mode/ptt_listener_linux.py by patches/apply.sh.
Run standalone: `python3 -m voice_mode.ptt_listener_linux`
"""
import asyncio
import logging
import os
import subprocess
import time
from typing import Optional

import psutil

from voice_mode import ptt_core, ptt_ipc

logger = logging.getLogger("voicemode.ptt_listener_linux")

POLL_INTERVAL = 0.05  # seconds; how often to check hold-threshold while held


def is_focused_terminal(terminal_pid: int) -> bool:
    """True if the currently-focused X11 window belongs to terminal_pid."""
    try:
        window = subprocess.run(
            ["xdotool", "getactivewindow"], capture_output=True, text=True,
            timeout=1, check=True,
        ).stdout.strip()
        focused_pid = subprocess.run(
            ["xdotool", "getwindowpid", window], capture_output=True, text=True,
            timeout=1, check=True,
        ).stdout.strip()
        return int(focused_pid) == terminal_pid
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError, subprocess.TimeoutExpired):
        return False


def resolve_terminal_pid(pid: Optional[int] = None) -> Optional[int]:
    """Walks the process tree upward from `pid` (default: this process) and
    returns the oldest ancestor before PID 1 — a best-effort proxy for "the
    terminal emulator that ultimately owns this session" (documented v1
    heuristic; see the design doc's known limitation on ambiguous titles)."""
    try:
        proc = psutil.Process(pid)
        last = proc
        while True:
            parent = last.parent()
            if parent is None or parent.pid <= 1:
                return last.pid
            last = parent
    except psutil.Error:
        return None


async def run_listener(port: int = ptt_ipc.DEFAULT_PORT) -> None:
    from pynput import keyboard

    terminal_pid = resolve_terminal_pid()
    if terminal_pid is None:
        logger.error("Could not resolve terminal PID — PTT listener not starting")
        return

    server = ptt_ipc.PTTEventServer(port=port)
    await server.start()
    logger.info(f"PTT relay listening on 127.0.0.1:{server.actual_port}")

    state = ptt_core.PTTKeyState(
        hold_threshold=float(os.getenv("VOICEMODE_PTT_HOLD_THRESHOLD", str(ptt_core.DEFAULT_HOLD_THRESHOLD)))
    )
    loop = asyncio.get_event_loop()
    held = False

    def _emit(action: ptt_core.PTTAction) -> None:
        asyncio.run_coroutine_threadsafe(
            ptt_ipc.send_event(ptt_ipc.PTTEvent(type=action.value, ts=time.monotonic()), port=server.actual_port),
            loop,
        )

    def _matches_hotkey(key) -> bool:
        # Control-Space: either Ctrl modifier held alongside Space.
        return key == keyboard.Key.space

    ctrl_down = False

    def on_press(key):
        nonlocal ctrl_down, held
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            ctrl_down = True
            return
        if ctrl_down and _matches_hotkey(key) and not held:
            if not is_focused_terminal(terminal_pid):
                return
            held = True
            _emit(state.on_press(time.monotonic()))

    def on_release(key):
        nonlocal ctrl_down, held
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            ctrl_down = False
            return
        if held and _matches_hotkey(key):
            held = False
            action = state.on_release(time.monotonic())
            if action is not None:
                _emit(action)

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()

    try:
        while True:
            if held:
                action = state.poll_hold(time.monotonic())
                if action is not None:
                    _emit(action)
            await asyncio.sleep(POLL_INTERVAL)
    finally:
        listener.stop()
        await server.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_listener())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ptt_listener_linux.py -v`
Expected: 4 passed

- [ ] **Step 5: Manual acceptance (requires a real X11 session — not available in WSL2; run on native Linux hardware or defer to Task 7's WSL2 path)**

This step needs a real X11 session with physical key input, which this WSL2 development machine does not have — run it on native Linux hardware, or fold it into Task 7's WSL2 acceptance pass (Task 7 Step 4) instead. Checklist, matching the design doc's manual-acceptance section:

```bash
cd /home/wunsch/git/voicemode-local
.venv/bin/python -m voice_mode.ptt_listener_linux
```

1. Hold Control-Space for over a second with the terminal focused, speak, release — the audio returned by `converse()` should contain exactly what was said, ending at release, not at a silence timeout.
2. Tap Control-Space briefly (under a second), speak, then tap Control-Space again before pausing — the recording should end immediately at the second tap rather than waiting for silence detection.
3. Click a different window so the terminal loses focus, then press Control-Space — confirm (via the listener's log output) that no event is emitted.

Record the outcome of these three checks in the task/PR notes; this step cannot be automated without real X11 input hardware.

- [ ] **Step 6: Commit**

```bash
git add patches/ptt_listener_linux.py tests/test_ptt_listener_linux.py
git commit -m "feat(ptt): native Linux (X11) key listener with focus scoping"
```

---

### Task 6: Config wiring, auto-start, and `voicemode-switch ptt status`

**Files:**
- Modify: `patches/apply.sh`
- Modify: `voicemode-switch`
- Test: manual (shell-script changes; existing repo has no test harness for `apply.sh`/`voicemode-switch` — verified by running them, matching the existing convention for these two files)

- [ ] **Step 1: Wire the new modules and patchers into `apply.sh`**

Read `patches/apply.sh` first to find the exact insertion point (immediately after the existing `patch_converse_queue.py` block, so ordering is: queue patch, then PTT — the PTT patcher's anchors assume the queue patch already ran, per Task 3's docstring).

Add, right after the existing block that copies `voice_queue.py` and runs `patch_converse_queue.py`:

```bash
# Install push-to-talk modules and patch converse.py + core.py to use them.
for f in ptt_core.py ptt_ipc.py ptt_bridge.py ptt_playback_bridge.py ptt_listener_linux.py; do
    if [ -f "$SCRIPT_DIR/$f" ]; then
        cp "$SCRIPT_DIR/$f" "$VM_DIR/$f"
        echo "[patches] Applied $f → $VM_DIR/$f"
    fi
done
if [ -f "$SCRIPT_DIR/patch_converse_ptt.py" ]; then
    PYBIN="$VENV_DIR/bin/python"
    [ -x "$PYBIN" ] || PYBIN="$VENV_DIR/Scripts/python.exe"
    [ -x "$PYBIN" ] || PYBIN="python3"
    "$PYBIN" "$SCRIPT_DIR/patch_converse_ptt.py" "$VM_DIR/tools/converse.py"
fi
if [ -f "$SCRIPT_DIR/patch_core_ptt.py" ]; then
    PYBIN="$VENV_DIR/bin/python"
    [ -x "$PYBIN" ] || PYBIN="$VENV_DIR/Scripts/python.exe"
    [ -x "$PYBIN" ] || PYBIN="python3"
    "$PYBIN" "$SCRIPT_DIR/patch_core_ptt.py" "$VM_DIR/core.py"
fi
```

- [ ] **Step 2: Run `apply.sh` and confirm both patches apply on a clean install**

```bash
git stash  # protect any in-progress manual edits made while testing Tasks 3-5
./patches/apply.sh
git stash pop
python3 -m pytest tests/ -v
```
Expected: all passed, `apply.sh` output shows both `[patch_converse_ptt] patched ...` and `[patch_core_ptt] patched ...` (or "already patched" if run twice)

- [ ] **Step 3: Add `voicemode-switch ptt status`**

Read `voicemode-switch` first to find the existing `queue` subcommand's implementation (it prints floor holder + queue order — a read-only status display) and follow the same style. Add a `ptt` subcommand with a `status` action:

```bash
# (inside voicemode-switch's existing subcommand dispatch, alongside the "queue" case)
ptt)
    case "${2:-status}" in
        status)
            enabled="${VOICEMODE_PTT_ENABLED:-false}"
            hotkey="${VOICEMODE_PTT_HOTKEY:-ctrl+space}"
            threshold="${VOICEMODE_PTT_HOLD_THRESHOLD:-1.0}"
            port="${VOICEMODE_PTT_PORT:-8765}"
            echo "Push-to-talk: enabled=$enabled hotkey=$hotkey hold_threshold=${threshold}s port=$port"
            if command -v nc >/dev/null 2>&1; then
                if nc -z 127.0.0.1 "$port" 2>/dev/null; then
                    echo "Listener: reachable on 127.0.0.1:$port"
                else
                    echo "Listener: NOT reachable on 127.0.0.1:$port — start it, or toggle VOICEMODE_PTT_ENABLED via update_config"
                fi
            fi
            ;;
        *)
            echo "Usage: voicemode-switch ptt status" >&2
            exit 1
            ;;
    esac
    ;;
```

Toggling `VOICEMODE_PTT_ENABLED` itself stays on the `update_config` MCP tool per the design's constraint — this subcommand is read-only status, matching `voicemode-switch queue`.

- [ ] **Step 4: Manually verify**

Run: `./voicemode-switch ptt status`
Expected: prints the four config values and a listener reachability line (unreachable, since no listener is running yet — expected before Task 5's listener is started)

- [ ] **Step 5: Commit**

```bash
git add patches/apply.sh voicemode-switch
git commit -m "feat(ptt): wire config, apply.sh, and voicemode-switch ptt status"
```

---

### Task 7: WSL2 Windows-side companion

**Files:**
- Create: `patches/ptt_listener_windows.py` (run on the Windows host, not inside WSL)
- Modify: `docs/windows-issues.md` or a new `docs/ptt-wsl-setup.md` — setup instructions (this is a manually-run companion script, not something `apply.sh` can install across the WSL/Windows boundary)

**Interfaces:**
- Consumes: `ptt_core.PTTKeyState`/`PTTAction` — same module, copied to the Windows side (no shared filesystem between the WSL guest and Windows host's Python site-packages, so this is a plain file copy, not an import across the boundary).
- Produces: connects out to `127.0.0.1:<port>` (forwarded into the WSL guest by `localhostForwarding=true`, already set in this machine's `.wslconfig` per the project's own WSL networking notes) and calls `ptt_ipc.send_event`-equivalent logic.

- [ ] **Step 1: Write the Windows companion script**

```python
# patches/ptt_listener_windows.py
"""Windows-host push-to-talk companion for WSL2.

Run this on the WINDOWS side (not inside WSL) with a Windows Python
install: `python ptt_listener_windows.py`. It captures Control-Space,
checks that the focused window is your WSL terminal (Windows Terminal,
by process name/title match), classifies press/hold/release the same way
patches/ptt_core.py does, and sends the classified events to
127.0.0.1:<port> — which WSL2's localhostForwarding delivers into the
Linux guest's relay server (patches/ptt_ipc.py / ptt_listener's role is
played by the guest-side relay started by `voicemode-switch` or apply.sh;
this script is ONLY the producer, it does not start a relay server itself).

Requires (Windows-side): pip install pynput pywin32

This file intentionally duplicates ptt_core.py's logic inline (copy-paste,
not import) since the WSL guest and Windows host do not share a Python
environment or filesystem path for imports.
"""
import ctypes
import json
import socket
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import win32gui
import win32process
from pynput import keyboard

PORT = 8765
HOLD_THRESHOLD = 1.0
POLL_INTERVAL = 0.05
TERMINAL_PROCESS_NAMES = {"WindowsTerminal.exe", "OpenConsole.exe"}


class PTTAction(Enum):
    PRESS = "press"
    SHORT_PRESS = "short_press"
    HOLD_START = "hold_start"
    HOLD_RELEASE = "hold_release"


@dataclass
class PTTKeyState:
    hold_threshold: float = HOLD_THRESHOLD
    _press_ts: Optional[float] = field(default=None, init=False)
    _hold_fired: bool = field(default=False, init=False)

    def on_press(self, ts):
        if self._press_ts is None:
            self._press_ts = ts
            self._hold_fired = False
        return PTTAction.PRESS

    def poll_hold(self, now):
        if self._press_ts is None or self._hold_fired:
            return None
        if now - self._press_ts >= self.hold_threshold:
            self._hold_fired = True
            return PTTAction.HOLD_START
        return None

    def on_release(self, ts):
        if self._press_ts is None:
            return None
        was_hold = self._hold_fired
        self._press_ts = None
        self._hold_fired = False
        return PTTAction.HOLD_RELEASE if was_hold else PTTAction.SHORT_PRESS


def focused_window_is_terminal() -> bool:
    hwnd = win32gui.GetForegroundWindow()
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    try:
        import psutil
        return psutil.Process(pid).name() in TERMINAL_PROCESS_NAMES
    except Exception:
        return False


def send_event(action: PTTAction) -> None:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1) as sock:
            sock.sendall((json.dumps({"type": action.value, "ts": time.time()}) + "\n").encode())
    except OSError:
        pass  # relay not reachable right now — drop the event, don't crash the listener


def main() -> None:
    state = PTTKeyState()
    held = False
    ctrl_down = False

    def on_press(key):
        nonlocal ctrl_down, held
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            ctrl_down = True
            return
        if ctrl_down and key == keyboard.Key.space and not held:
            if not focused_window_is_terminal():
                return
            held = True
            send_event(state.on_press(time.monotonic()))

    def on_release(key):
        nonlocal ctrl_down, held
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            ctrl_down = False
            return
        if held and key == keyboard.Key.space:
            held = False
            action = state.on_release(time.monotonic())
            if action is not None:
                send_event(action)

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()
    print(f"WSL2 push-to-talk companion running — forwarding to 127.0.0.1:{PORT}")
    try:
        while True:
            if held:
                action = state.poll_hold(time.monotonic())
                if action is not None:
                    send_event(action)
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Start a relay server on the WSL2/Linux side for the companion to reach**

The Linux-side relay server needs to run even though `ptt_listener_linux.py` (Task 5) won't work under WSLg (no real X11 terminal window there). Add a minimal server-only entry point:

```python
# append to patches/ptt_listener_linux.py

async def run_relay_only(port: int = ptt_ipc.DEFAULT_PORT) -> None:
    """WSL2 mode: no local key capture (the Windows companion does that);
    just run the relay server that both the Windows companion and
    converse() connect to."""
    server = ptt_ipc.PTTEventServer(port=port)
    await server.start()
    logger.info(f"PTT relay (WSL2 mode, no local capture) listening on 127.0.0.1:{server.actual_port}")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await server.stop()
```

Add a CLI flag to the module's `if __name__ == "__main__":` block:

```python
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if "--relay-only" in sys.argv:
        asyncio.run(run_relay_only())
    else:
        asyncio.run(run_listener())
```

- [ ] **Step 3: Write the setup doc**

```markdown
# docs/ptt-wsl-setup.md
# Push-to-Talk on WSL2 — Setup

Push-to-talk needs two processes when running under WSL2, because your
terminal (Windows Terminal) is a Windows process while voice-mode runs
inside the Linux guest:

1. **Linux side (relay only, no key capture):**
   ```bash
   cd /home/wunsch/git/voicemode-local
   .venv/bin/python -m voice_mode.ptt_listener_linux --relay-only &
   ```
2. **Windows side (actual key capture):** from a Windows Python install
   (not WSL's Python):
   ```powershell
   pip install pynput pywin32 psutil
   python \\wsl$\Ubuntu\home\wunsch\git\voicemode-local\patches\ptt_listener_windows.py
   ```
   (Adjust the `\\wsl$\...` path to your distro name if not `Ubuntu`.)

This relies on this machine's `.wslconfig` already having
`localhostForwarding=true` (see `docs/windows-issues.md` and this
project's WSL networking notes) — the Windows-side script connects to
`127.0.0.1:8765`, which WSL2 forwards into the Linux guest where the
relay server is listening.

Then set `VOICEMODE_PTT_ENABLED=true` via the `update_config` MCP tool
(ask Claude: "turn on push to talk") — no restart needed for future
`converse()` calls.
```

- [ ] **Step 4: Manual acceptance**

Follow `docs/ptt-wsl-setup.md` end to end on this machine: start both processes, enable PTT via `update_config`, start a voice conversation, and verify hold-to-talk, short-press-with-manual-stop, and barge-in all work with the Windows Terminal window focused, and that switching focus to another Windows app suppresses the hotkey.

- [ ] **Step 5: Commit**

```bash
git add patches/ptt_listener_windows.py patches/ptt_listener_linux.py docs/ptt-wsl-setup.md
git commit -m "feat(ptt): WSL2 Windows-side companion + relay-only Linux mode"
```

---

## Out of scope for this plan (follow-up work)

- **macOS and native Windows key listeners.** Both reuse `ptt_core.py` and `ptt_ipc.py` entirely unchanged; only a new listener module analogous to Task 5 (macOS: `pyobjc` for focus, `pynput`'s macOS backend; requires a one-time Accessibility permission grant that can't be automated) or Task 7's Windows script running locally instead of over the WSL boundary is needed. Write a short follow-up plan once one of those platforms is actually in use — see the design doc's "Out of scope" section.
- **Pure-Wayland native Linux** (no XWayland) — the design doc's documented terminal-keybinding fallback, not the OS-hook approach used here.
- Disambiguating multiple concurrent PTT-enabled sessions sharing one relay port (v1 accepted limitation).
