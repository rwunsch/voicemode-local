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
    # Set directly, never restored: each test loads its own copy of the module, and an exit
    # thread it started (mark_wedged schedules one 30 s ahead) may outlive the test. With
    # monkeypatch the real os._exit came back at teardown and ended pytest (rc 75, 2026-11-27).
    mod._exit_now = lambda code=mod.EXIT_CODE: exits.append(code)
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


# Code review of 33d87cb, finding 1: a chime that missed its deadline was swallowed by
# upstream's `except Exception`, so the turn went on with the dead device, every later chime
# waited its own deadline again, and the exit came only with the next converse.
def test_once_wedged_every_device_call_fails_at_once(ad, monkeypatch):
    ad.mark_wedged("playback did not finish within 10 s")
    t0 = time.monotonic()
    with pytest.raises(ad.DeadlineMissed):
        asyncio.run(ad.call(lambda: time.sleep(3600), 30, "probe"))
    with pytest.raises(ad.DeadlineMissed):
        asyncio.run(ad.play(FakePlayer(), [0.0] * 2400, 24000))
    assert time.monotonic() - t0 < 0.5


def test_being_wedged_schedules_the_exit_even_if_the_turn_never_returns(ad, monkeypatch):
    monkeypatch.setattr(ad, "WEDGED_EXIT_CAP_S", 0.3)
    ad.mark_wedged("probe")
    deadline = time.monotonic() + 3
    while not ad.exits and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ad.exits == [ad.EXIT_CODE]


def test_an_earlier_exit_wins_over_the_cap(ad, monkeypatch):
    monkeypatch.setattr(ad, "WEDGED_EXIT_CAP_S", 30)
    monkeypatch.setattr(ad, "EXIT_DELAY_S", 0.2)
    ad.mark_wedged("probe")            # cap: 30 s
    assert ad.wedged_result()          # the reply went out: exit in 0.2 s
    deadline = time.monotonic() + 3
    while not ad.exits and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ad.exits == [ad.EXIT_CODE]


def test_guard_ends_a_turn_that_hangs_after_a_swallowed_miss(ad, monkeypatch):
    # The shape of upstream's turn: a chime whose miss is swallowed, then a wait with no
    # deadline (TTS playback on to_thread). The wrapper must answer at once, not hang.
    monkeypatch.setattr(ad, "EXIT_DELAY_S", 30)  # no exit during the test
    monkeypatch.setenv(ad.DEADLINE_SETTING, "0.2")

    async def turn():
        try:
            await ad.play(FakePlayer(hang=True), [0.0] * 2400, 24000)
        except Exception:
            pass                                   # upstream: logger.debug, return False
        await asyncio.sleep(3600)                  # the TTS wait that never ends
        return "never"

    t0 = time.monotonic()
    out = asyncio.run(ad.guard(turn()))
    assert out.startswith("Voice stopped: the audio device did not respond")
    assert time.monotonic() - t0 < 3


def test_guard_passes_a_normal_result_through(ad):
    async def turn():
        return "ok"
    assert asyncio.run(ad.guard(turn())) == "ok"


# Code review of 33d87cb, finding 4: a new voice server deleted the (empty, open) backstop
# file of a voice server still running; on POSIX its later dump would go to an unlinked file.
def test_a_live_servers_empty_backstop_file_is_kept(ad, monkeypatch, tmp_path):
    d = tmp_path / "logs" / "stall"
    d.mkdir(parents=True)
    live = d / f"backstop-{os.getppid()}.txt"
    live.write_text("")
    f = ad.open_backstop()
    f.close()
    assert live.exists()


def test_backstop_rearm_is_not_every_beat(ad, monkeypatch):
    calls = []
    monkeypatch.setattr(ad.faulthandler, "dump_traceback_later", lambda *a, **k: calls.append(a))
    clock = Clock()
    dog = ad.Watchdog(90, clock=clock)
    for _ in range(25):
        dog.stamp()
        clock.t += 1
    assert len(calls) == 3   # at 0, 10 and 20 s


# Talos on FELIX #2104 (1): the retention prune counted live servers' files too, so with more
# than KEEP_DUMPS other backstop files the oldest LIVE server's open file was unlinked.
def test_the_retention_prune_never_removes_a_live_servers_file(ad, monkeypatch, tmp_path):
    d = tmp_path / "logs" / "stall"
    d.mkdir(parents=True)
    live = [d / f"backstop-{100000 + i}.txt" for i in range(ad.KEEP_DUMPS + 1)]
    fired = [d / f"backstop-{900000 + i}.txt" for i in range(ad.KEEP_DUMPS + 2)]
    for n, p in enumerate(live + fired):
        p.write_text("" if p in live else "Current thread")
        os.utime(p, (1000 + n, 1000 + n))   # the live ones are the oldest
    monkeypatch.setattr(ad, "_pid_alive", lambda raw: int(raw) < 900000)
    f = ad.open_backstop()
    f.close()
    assert all(p.exists() for p in live)
    kept = [p for p in fired if p.exists()]
    assert kept == fired[-ad.KEEP_DUMPS:]   # fired files of dead runs: the newest KEEP_DUMPS


def test_on_windows_the_prune_relies_on_open_files_being_undeletable(ad, monkeypatch, tmp_path):
    # Windows cannot ask whether a pid runs; the delete is tried and an open file refuses it.
    d = tmp_path / "logs" / "stall"
    d.mkdir(parents=True)
    fired = [d / f"backstop-{900000 + i}.txt" for i in range(ad.KEEP_DUMPS + 2)]
    for n, p in enumerate(fired):
        p.write_text("Current thread")
        os.utime(p, (1000 + n, 1000 + n))
    monkeypatch.setattr(ad, "_keep_as_live", lambda raw: False)   # as on Windows
    f = ad.open_backstop()
    f.close()
    assert [p for p in fired if p.exists()] == fired[-ad.KEEP_DUMPS:]
