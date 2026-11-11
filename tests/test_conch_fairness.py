"""Fair holds, an MCP progress heartbeat, and a logged cancelled wait
(patches/conch_hold.py, patches/mcp_progress.py, patches/patch_conch_fairness.py).

Live 2026-11-11 (FELIX, Copilot): a session holding the mic with hold_conch kept another
waiting with no end, and Copilot cancels a tool call after 180 s unless progress arrives."""
import asyncio
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent.parent / "patches"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, PATCHES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ch = _load("conch_hold")
mp = _load("mcp_progress")


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.delenv(ch.MAX_TURNS_SETTING, raising=False)
    monkeypatch.delenv(mp.INTERVAL_SETTING, raising=False)
    ch.reset()


# ---- conch_hold ----

def test_no_hold_asked_is_no_hold():
    assert ch.effective_hold(False, "S", replied=True, waiting=True) is False


def test_a_hold_with_nobody_waiting_stands_however_long():
    assert all(ch.effective_hold(True, "S", replied=False, waiting=False) for _ in range(10))


def test_a_held_turn_without_a_reply_releases_when_someone_waits():
    assert ch.effective_hold(True, "S", replied=False, waiting=True) is False


def test_a_hold_lasts_three_replied_turns_while_someone_waits():
    got = [ch.effective_hold(True, "S", replied=True, waiting=True) for _ in range(4)]
    assert got == [True, True, True, False]


def test_turns_held_before_anyone_waited_do_not_count():
    for _ in range(5):
        assert ch.effective_hold(True, "S", replied=True, waiting=False)
    got = [ch.effective_hold(True, "S", replied=True, waiting=True) for _ in range(4)]
    assert got == [True, True, True, False]


def test_the_count_starts_again_after_a_release():
    for _ in range(4):
        ch.effective_hold(True, "S", replied=True, waiting=True)
    ch.effective_hold(False, "S", replied=True, waiting=True)
    assert ch.effective_hold(True, "S", replied=True, waiting=True) is True


def test_sessions_are_counted_apart():
    for _ in range(3):
        ch.effective_hold(True, "S", replied=True, waiting=True)
    assert ch.effective_hold(True, "T", replied=True, waiting=True) is True


def test_the_cap_is_configurable(monkeypatch):
    monkeypatch.setenv(ch.MAX_TURNS_SETTING, "1")
    got = [ch.effective_hold(True, "S", replied=True, waiting=True) for _ in range(2)]
    assert got == [True, False]


@pytest.mark.parametrize("raw", ["x", "-2", ""])
def test_a_bad_cap_falls_back_to_three(monkeypatch, raw):
    monkeypatch.setenv(ch.MAX_TURNS_SETTING, raw)
    assert ch.max_turns() == 3


def test_an_unknown_queue_is_read_as_nobody_waiting(monkeypatch):
    # Outside the package (here) the queue cannot be read: upstream behaviour, hold stands.
    assert ch.others_waiting("S") is False
    assert ch.effective_hold(True, "S", replied=False) is True


# ---- mcp_progress ----

class FakeCtx:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def report_progress(self, progress, total=None, message=None):
        self.calls.append((progress, message))
        if self.fail:
            raise RuntimeError("client gone")


def _run(coro):
    return asyncio.run(coro)


def test_progress_is_sent_while_the_call_runs():
    ctx = FakeCtx()

    async def go():
        async with mp.heartbeat(ctx_getter=lambda: ctx, interval=0.05):
            await asyncio.sleep(0.18)
    _run(go())
    assert [p for p, _ in ctx.calls] == [1, 2, 3]
    assert all(m for _, m in ctx.calls)


def test_progress_stops_when_the_call_ends():
    ctx = FakeCtx()

    async def go():
        async with mp.heartbeat(ctx_getter=lambda: ctx, interval=0.05):
            await asyncio.sleep(0.06)
        n = len(ctx.calls)
        await asyncio.sleep(0.15)
        return n
    n_at_end = _run(go())
    assert n_at_end == 1
    assert len(ctx.calls) == n_at_end


def test_no_context_means_no_heartbeat():
    async def go():
        async with mp.heartbeat(ctx_getter=lambda: None, interval=0.01):
            await asyncio.sleep(0.03)
        return "ok"
    assert _run(go()) == "ok"


def test_a_failing_context_lookup_never_breaks_the_call():
    def boom():
        raise RuntimeError("No active context found.")

    async def go():
        async with mp.heartbeat(ctx_getter=boom, interval=0.01):
            return "ok"
    assert _run(go()) == "ok"


def test_a_failing_report_never_breaks_the_call():
    ctx = FakeCtx(fail=True)

    async def go():
        async with mp.heartbeat(ctx_getter=lambda: ctx, interval=0.02):
            await asyncio.sleep(0.07)
        return "ok"
    assert _run(go()) == "ok"
    assert len(ctx.calls) >= 2                 # it keeps trying


def test_an_error_in_the_call_propagates_and_stops_the_beat():
    ctx = FakeCtx()

    async def go():
        async with mp.heartbeat(ctx_getter=lambda: ctx, interval=0.02):
            raise ValueError("converse failed")
    with pytest.raises(ValueError):
        _run(go())


def test_interval_zero_disables(monkeypatch):
    monkeypatch.setenv(mp.INTERVAL_SETTING, "0")
    ctx = FakeCtx()

    async def go():
        async with mp.heartbeat(ctx_getter=lambda: ctx):
            await asyncio.sleep(0.02)
    _run(go())
    assert ctx.calls == []


@pytest.mark.parametrize("raw,want", [("30", 30.0), ("x", 30.0), ("-1", 30.0), ("5", 5.0)])
def test_interval_setting(monkeypatch, raw, want):
    monkeypatch.setenv(mp.INTERVAL_SETTING, raw)
    assert mp.interval_s() == want


# ---- the converse patch ----

@pytest.fixture
def pristine_converse(tmp_path):
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    target = tmp_path / "converse.py"
    shutil.copyfile(Path(raw) / "converse.py", target)
    return target


def _apply(target):
    return subprocess.run([sys.executable, str(PATCHES / "patch_conch_fairness.py"), str(target)],
                          capture_output=True, text=True)


def test_the_patch_applies_once_and_compiles(pristine_converse):
    assert _apply(pristine_converse).returncode == 0
    text = pristine_converse.read_text()
    compile(text, "converse.py", "exec")
    assert "VML: CONCH FAIRNESS" in text
    assert "conch.release(hold=_vml_hold)" in text
    assert '"result": "cancelled"' in text
    assert "async with _vml_heartbeat():" in text
    assert _apply(pristine_converse).returncode == 0
    assert pristine_converse.read_text() == text


def test_drift_fails_and_writes_nothing(pristine_converse):
    text = pristine_converse.read_text().replace("            held_seconds = conch.release(hold=hold_conch)\n", "")
    pristine_converse.write_text(text)
    assert _apply(pristine_converse).returncode != 0
    assert pristine_converse.read_text() == text


@pytest.mark.parametrize("text,want", [
    ("yes", True), ("Yes again.", True), ("ja", True),
    (".\n.\n.\n.\n.", False),          # live 2026-11-11 21:29: whisper's output for silence
    ("", False), (None, False), ("  ...  ", False), ("-", False),
])
def test_only_words_count_as_a_reply(text, want):
    assert ch.is_reply(text) is want
