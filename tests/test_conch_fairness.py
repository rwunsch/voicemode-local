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
            await asyncio.sleep(0.5)  # wide: under full-suite load a 0.06 s window saw 0 beats
        n = len(ctx.calls)
        await asyncio.sleep(0.25)
        return n
    n_at_end = _run(go())
    assert n_at_end >= 1                  # it beat while the call ran
    assert len(ctx.calls) == n_at_end     # and not once after it ended


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
        # 0.5 s of 0.02 s beats: >= 2 calls holds under load (final review M7; it was a 0.07 s
        # window, which a slow event loop could leave with one beat).
        async with mp.heartbeat(ctx_getter=lambda: ctx, interval=0.02):
            await asyncio.sleep(0.5)
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


# ---- final review 2026-11-11 ----

class E:
    def __init__(self, sid, mode):
        self.session_id, self.mode = sid, mode


def test_only_a_blocked_waiter_counts_as_waiting():
    # Important 2: a busy callback session keeps working; counting it cut every hold short
    # and added ~10 s of dead air per turn.
    assert ch.blocked_waiters([E("A", "callback")], "S") == []
    assert [e.session_id for e in ch.blocked_waiters([E("A", "callback"), E("W", "wait")], "S")] == ["W"]
    assert ch.blocked_waiters([E("S", "wait")], "S") == []


def test_a_lapsed_hold_starts_the_count_again():
    # Minor 1: the count survived a hold that lapsed by its TTL, so the next hold got fewer turns.
    assert ch.effective_hold(True, "S", replied=True, waiting=True, acquired_at=0.0, now=1.0)
    assert ch.effective_hold(True, "S", replied=True, waiting=True, acquired_at=2.0, now=3.0)
    # 20 s later: the hold kept at 3.0 lapsed at 13.0; this acquire is a fresh floor
    got = [ch.effective_hold(True, "S", replied=True, waiting=True, acquired_at=t, now=t + 1)
           for t in (20.0, 22.0, 24.0, 26.0)]
    assert got == [True, True, True, False]


def test_a_resumed_hold_keeps_counting():
    for t in (0.0, 2.0, 4.0):
        assert ch.effective_hold(True, "S", replied=True, waiting=True, acquired_at=t, now=t + 1)
    assert ch.effective_hold(True, "S", replied=True, waiting=True, acquired_at=6.0, now=7.0) is False


def test_an_outer_cancel_during_cleanup_is_not_swallowed():
    # Minor 2: suppress(CancelledError) around `await task` also ate the caller's cancel.
    class Lingering:
        async def report_progress(self, progress, total=None, message=None):
            try:
                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                await asyncio.sleep(0.3)     # a slow send that outlives the first cancel

    async def body():
        async with mp.heartbeat(ctx_getter=lambda: Lingering(), interval=0.01):
            await asyncio.sleep(0.05)        # a beat is in flight when the call ends

    async def go():
        t = asyncio.get_running_loop().create_task(body())
        await asyncio.sleep(0.1)             # body done; cleanup awaits the lingering beat
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    _run(go())


def test_the_patch_logs_the_hold_it_applied_and_exempts_speak_only_turns(pristine_converse):
    assert _apply(pristine_converse).returncode == 0
    text = pristine_converse.read_text()
    assert '"hold_active": bool(_vml_hold)' in text
    assert '"turn_results" in locals()' in text and "not wait_for_response" in text
    assert "acquired_at=" in text


def test_the_patch_names_a_reserved_free_mic(pristine_converse):
    assert _apply(pristine_converse).returncode == 0
    assert "_vml_cw.reserved_label()" in pristine_converse.read_text()


@pytest.mark.parametrize("script,name,marker", [
    ("patch_conch_fairness.py", "converse.py", "VML: CONCH FAIRNESS"),
    ("patch_conch_wakers.py", "conch_queue.py", "VML: CONCH WAKERS"),
])
def test_an_older_patch_text_is_reported_not_silently_kept(tmp_path, script, name, marker):
    # v1.1.1 final review minor: the marker alone decided "applied", so a venv patched with
    # an older text silently kept it. It still is not re-patched, but it says so.
    target = tmp_path / name
    body = f"# {marker}: an older version of this patch\nx = 1\n"
    target.write_text(body)
    done = subprocess.run([sys.executable, str(PATCHES / script), str(target)], capture_output=True, text=True)
    assert done.returncode == 0
    assert "older version" in done.stderr and "reinstall" in done.stderr
    assert target.read_text() == body


def test_a_current_patch_is_quiet_on_the_second_pass(pristine_converse):
    assert _apply(pristine_converse).returncode == 0
    again = _apply(pristine_converse)
    assert again.returncode == 0 and again.stderr == ""


@pytest.mark.skipif(not __import__("os").environ.get("VM812_SRC"), reason="VM812_SRC is not set")
def test_the_tool_description_says_to_queue_again_rather_than_stop(tmp_path):
    """Windows 2026-11-13: a Copilot chat 'released the voice channel' for another session
    and then stopped talking. The description every harness reads must say how to share
    the mic without leaving: call converse again with wait_for_conch=true."""
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path
    t = tmp_path / "converse.py"
    shutil.copy(Path(os.environ["VM812_SRC"]) / "converse.py", t)
    patcher = Path(__file__).resolve().parent.parent / "patches" / "patch_conch_fairness.py"
    r = subprocess.run([sys.executable, str(patcher), str(t)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    src = t.read_text(encoding="utf-8")
    doc = src[src.index('"""Have an ongoing voice conversation'):]
    doc = doc[:doc.index('"""', 3)]
    assert "wait_for_conch=true" in doc
    assert "Only the user ends a voice conversation" in doc
