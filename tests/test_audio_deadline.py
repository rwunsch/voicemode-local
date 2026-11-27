"""audio_deadline: audio-device calls off the event loop with a deadline, and the loop
watchdog (FELIX #2095: the voice server answered nothing for minutes, pill on Speaking)."""
import asyncio
import faulthandler
import importlib.util
import threading
import time
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent / "patches" / "audio_deadline.py"


@pytest.fixture
def ad(monkeypatch):
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


def test_a_blocked_loop_is_caught_by_the_real_thread(ad, monkeypatch):
    monkeypatch.setenv(ad.STALL_SETTING, "0.5")
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


def test_loop_lag_is_none_before_start(ad):
    assert ad.loop_lag() is None
