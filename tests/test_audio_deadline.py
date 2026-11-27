"""audio_deadline: audio-device calls off the event loop with a deadline, and the loop
watchdog (FELIX #2095: the voice server answered nothing for minutes, pill on Speaking)."""
import asyncio
import os
import faulthandler
import importlib.util
import threading
import time
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent / "patches" / "audio_deadline.py"


@pytest.fixture
def ad(monkeypatch, tmp_path):
    # Every dump and backstop file goes under the test's own folder, never ~/.voicemode.
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("audio_deadline_under_test", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    exits = []
    monkeypatch.setattr(mod, "_exit_now", lambda code=mod.EXIT_CODE: exits.append(code))
    mod.exits = exits
    yield mod
    faulthandler.cancel_dump_traceback_later()


class FakePlayer:
    """NonBlockingAudioPlayer's surface. ``hang`` = the device never calls back."""

    def __init__(self, hang=False, stop_hangs=False):
        self.hang, self.stop_hangs = hang, stop_hangs
        self.playback_complete = threading.Event()
        self.calls = []

    def play(self, samples, rate, blocking=False):
        self.calls.append(("play", blocking, threading.current_thread().name))
        if not self.hang:
            self.playback_complete.set()
        if blocking:
            self.wait()

    def wait(self, timeout=None):
        self.playback_complete.wait(timeout)
        if self.stop_hangs:
            time.sleep(3600)


def test_call_returns_the_value_and_runs_off_the_loop(ad):
    async def go():
        return await ad.call(lambda: threading.current_thread().name, 2, "probe")
    assert asyncio.run(go()) == "vml-audio-probe"
    assert ad.wedged is None


def test_call_passes_the_error_on(ad):
    def boom():
        raise ValueError("no device")

    async def go():
        await ad.call(boom, 2, "probe")
    with pytest.raises(ValueError, match="no device"):
        asyncio.run(go())
    assert ad.wedged is None


def test_a_hung_call_misses_its_deadline_and_the_loop_keeps_turning(ad):
    ticks = []

    async def ticker():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.05)

    async def go():
        t = asyncio.ensure_future(ticker())
        try:
            await ad.call(lambda: time.sleep(3600), 0.4, "refresh audio devices")
        finally:
            t.cancel()
    t0 = time.monotonic()
    with pytest.raises(ad.DeadlineMissed):
        asyncio.run(go())
    assert time.monotonic() - t0 < 2
    assert len(ticks) >= 4  # the loop served others while the call hung
    assert "refresh audio devices" in ad.wedged


def test_zero_deadline_runs_on_the_loop_as_before(ad, monkeypatch):
    monkeypatch.setenv(ad.DEADLINE_SETTING, "0")
    p = FakePlayer()
    asyncio.run(ad.play(p, [0.0] * 2400, 24000))
    assert p.calls == [("play", True, "MainThread")]


def test_play_finishes_normally(ad, monkeypatch):
    monkeypatch.setenv(ad.DEADLINE_SETTING, "1")
    p = FakePlayer()
    asyncio.run(ad.play(p, [0.0] * 2400, 24000))
    assert p.calls == [("play", False, "vml-audio-playback")]
    assert ad.wedged is None


def test_a_device_that_never_calls_back_is_a_miss(ad, monkeypatch):
    # upstream's play(blocking=True) waits forever here, on the event loop.
    monkeypatch.setenv(ad.DEADLINE_SETTING, "0.3")
    t0 = time.monotonic()
    with pytest.raises(ad.DeadlineMissed):
        asyncio.run(ad.play(FakePlayer(hang=True), [0.0] * 2400, 24000))
    assert time.monotonic() - t0 < 2
    assert "playback did not finish" in ad.wedged


def test_a_close_that_hangs_is_a_miss(ad, monkeypatch):
    monkeypatch.setenv(ad.DEADLINE_SETTING, "0.3")
    with pytest.raises(ad.DeadlineMissed):
        asyncio.run(ad.play(FakePlayer(stop_hangs=True), [0.0] * 2400, 24000))
    assert "playback did not return" in ad.wedged


@pytest.mark.parametrize("raw,want", [("", 10.0), ("abc", 10.0), ("-1", 10.0), ("nan", 10.0),
                                      ("inf", 10.0), ("0", 0.0), ("2.5", 2.5)])
def test_deadline_setting(ad, monkeypatch, raw, want):
    monkeypatch.setenv(ad.DEADLINE_SETTING, raw)
    assert ad.deadline_slack() == want


def test_wedged_result_says_what_to_do_and_exits_once(ad, monkeypatch):
    monkeypatch.setattr(ad, "EXIT_DELAY_S", 0.05)
    assert ad.wedged_result() is None
    ad.mark_wedged("playback did not finish within 10 s")
    msg = ad.wedged_result()
    assert "restart the chat" in msg and "playback did not finish" in msg
    ad.wedged_result()
    deadline = time.monotonic() + 1  # EXIT_DELAY_S is read when the exit is scheduled
    while not ad.exits and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.2)
    assert ad.exits == [ad.EXIT_CODE]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_watchdog_fires_after_the_limit(ad):
    clock, fired = Clock(), []
    dog = ad.Watchdog(90, clock=clock, exit_fn=lambda: fired.append(clock.t))
    for _ in range(89):
        clock.t += 1
        assert dog.check() is False
    clock.t += 2
    assert dog.check() is True and fired == [clock.t]
    clock.t += 1
    assert dog.check() is False  # once


def test_watchdog_stamp_keeps_it_quiet(ad):
    clock = Clock()
    dog = ad.Watchdog(5, clock=clock, exit_fn=lambda: pytest.fail("fired"))
    for _ in range(100):
        clock.t += 1
        dog.stamp()
        assert dog.check() is False
    assert dog.lag() == 0


def test_watchdog_after_sleep_does_not_fire(ad):
    # Windows' monotonic clock runs on through sleep; the checking thread was asleep too.
    clock = Clock()
    dog = ad.Watchdog(90, clock=clock, exit_fn=lambda: pytest.fail("fired after sleep"))
    dog.check()
    clock.t += 3600
    assert dog.check() is False
    clock.t += 1
    assert dog.check() is False


def test_watchdog_off_never_fires(ad):
    clock = Clock()
    dog = ad.Watchdog(0, clock=clock, exit_fn=lambda: pytest.fail("fired"))
    for _ in range(10):
        clock.t += 1
        assert dog.check() is False
    clock.t += 500
    assert dog.check() is False


def test_a_blocked_loop_is_caught_by_the_real_thread(ad, monkeypatch, tmp_path):
    monkeypatch.setenv(ad.STALL_SETTING, "0.5")
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path))  # its backstop file goes here
    fired = []

    async def go():
        ad.ensure_started()
        ad._watchdog.beat_s = 0.1
        ad._watchdog.exit_fn = lambda: fired.append(True)
        await asyncio.sleep(0.3)
        assert ad.loop_lag() < 0.5
        time.sleep(1.5)  # the hang: nothing on this loop runs
    asyncio.run(go())
    assert fired == [True]
    assert (tmp_path / "logs" / "stall" / f"backstop-{os.getpid()}.txt").exists()
    assert list((tmp_path / "logs" / "stall").glob("stall-*.txt"))  # the dump


def test_loop_lag_is_none_before_start(ad):
    assert ad.loop_lag() is None


# FELIX-voice review of f2d6b79: under FELIX voice-mode logs only to stderr (no file unless
# VOICEMODE_DEBUG), so the stack dump would be lost. It goes to a file of its own.
def _stall_dir(tmp_path):
    return tmp_path / "logs" / "stall"


def test_a_stack_dump_goes_to_a_file_of_its_own(ad, monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path))
    ad.dump_stacks("the voice server's event loop was stuck for 91 s", lag=91.2)
    (f,) = _stall_dir(tmp_path).glob("stall-*.txt")
    import re
    assert re.fullmatch(r"stall-\d{8}T\d{6}Z(-\d+)?\.txt", f.name)
    text = f.read_text()
    assert "loop was stuck for 91 s" in text and "loop_lag_s: 91.2" in text
    assert "MainThread" in text and "test_a_stack_dump_goes_to_a_file_of_its_own" in text
    assert "voice-mode pid:" in text


def test_only_the_last_five_dumps_are_kept(ad, monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path))
    d = _stall_dir(tmp_path)
    d.mkdir(parents=True)
    for i in range(7):
        p = d / f"stall-2026010{i}T000000Z.txt"
        p.write_text("old")
        os.utime(p, (1000 + i, 1000 + i))
    ad.dump_stacks("probe")
    names = sorted(p.name for p in d.glob("stall-*.txt"))
    assert len(names) == ad.KEEP_DUMPS == 5
    assert "stall-20260100T000000Z.txt" not in names


def test_the_backstop_file_is_opened_in_advance_and_empty_ones_are_pruned(ad, monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path))
    d = _stall_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "backstop-999999999.txt").write_text("")              # an old run, nothing written
    (d / "backstop-999999998.txt").write_text("Current thread")  # an old run that fired: kept
    f = ad.open_backstop()
    assert f is not None and Path(f.name).name == f"backstop-{os.getpid()}.txt"
    left = {p.name for p in d.glob("backstop-*.txt")}
    assert left == {"backstop-999999998.txt", f"backstop-{os.getpid()}.txt"}
    f.close()


def test_a_dump_never_fails_when_the_folder_cannot_be_written(ad, monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(blocker))  # logs/ cannot be made under a file
    ad.dump_stacks("probe")   # no exception
    assert ad.open_backstop() is None
