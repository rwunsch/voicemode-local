"""Tests for patches/voice_queue.py — cross-session voice queue."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

PATCHES_DIR = Path(__file__).parent.parent / "patches"
sys.path.insert(0, str(PATCHES_DIR))

import voice_queue  # noqa: E402


# ---------- process identity ----------

def test_process_start_time_self():
    st = voice_queue.process_start_time(os.getpid())
    assert st is not None
    # Stable across calls
    assert voice_queue.process_start_time(os.getpid()) == st


def test_pid_alive_self():
    st = voice_queue.process_start_time(os.getpid())
    assert voice_queue.pid_alive(os.getpid(), st) is True


def test_pid_alive_dead_process():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = proc.pid
    proc.wait()
    # Branch 1: start_time mismatch => always dead
    assert voice_queue.pid_alive(pid, start_time=-1) is False
    # Branch 2: no start_time, PID no longer exists (ProcessLookupError path)
    assert voice_queue.pid_alive(pid, start_time=None) is False


def test_pid_alive_start_time_mismatch():
    # PID alive but start_time from a different (recycled) process -> dead
    assert voice_queue.pid_alive(os.getpid(), start_time=-12345) is False


def test_pid_alive_none_start_time_falls_back_to_kill():
    assert voice_queue.pid_alive(os.getpid(), start_time=None) is True


# ---------- tickets ----------

def _my_st():
    return voice_queue.process_start_time(os.getpid())


def test_create_ticket_and_list(tmp_path):
    name = voice_queue.create_ticket(tmp_path, "projA", "af_bella")
    tickets = voice_queue.list_tickets(tmp_path)
    assert len(tickets) == 1
    tname, tdata = tickets[0]
    assert tname == name
    assert tdata["pid"] == os.getpid()
    assert tdata["start_time"] == _my_st()
    assert tdata["project"] == "projA"
    assert tdata["voice"] == "af_bella"


def test_one_ticket_per_pid(tmp_path):
    first = voice_queue.create_ticket(tmp_path, "projA", "af_bella")
    second = voice_queue.create_ticket(tmp_path, "projA", "af_bella")
    tickets = voice_queue.list_tickets(tmp_path)
    assert [t[0] for t in tickets] == [second]
    assert not (tmp_path / "queue" / f"{first}.json").exists()


def test_list_tickets_gc_dead_pid(tmp_path):
    qdir = tmp_path / "queue"
    qdir.mkdir()
    (qdir / "000000000000001-999999999.json").write_text(json.dumps(
        {"pid": 999999999, "start_time": 1, "project": "x", "voice": "v",
         "created": "now", "last_seen": time.time()}))
    assert voice_queue.list_tickets(tmp_path) == []
    assert list(qdir.glob("*.json")) == []


def test_list_tickets_gc_pid_reuse(tmp_path):
    # Live pid (ours) but wrong start_time => recycled pid => GC'd
    qdir = tmp_path / "queue"
    qdir.mkdir()
    (qdir / f"000000000000001-{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "start_time": -12345, "project": "x", "voice": "v",
         "created": "now", "last_seen": time.time()}))
    assert voice_queue.list_tickets(tmp_path) == []


def test_list_tickets_gc_stale_last_seen(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_queue, "TICKET_STALE", 0.1)
    voice_queue.create_ticket(tmp_path, "projA", "af_bella")
    time.sleep(0.2)
    assert voice_queue.list_tickets(tmp_path) == []


def test_list_tickets_gc_corrupt_json(tmp_path):
    qdir = tmp_path / "queue"
    qdir.mkdir()
    (qdir / "000000000000001-1234.json").write_text("{not json")
    assert voice_queue.list_tickets(tmp_path) == []
    assert list(qdir.glob("*.json")) == []


def test_heartbeat_ticket(tmp_path):
    name = voice_queue.create_ticket(tmp_path, "p", "v")
    before = voice_queue.list_tickets(tmp_path)[0][1]["last_seen"]
    time.sleep(0.05)
    assert voice_queue.heartbeat_ticket(tmp_path, name) is True
    after = voice_queue.list_tickets(tmp_path)[0][1]["last_seen"]
    assert after > before


def test_heartbeat_missing_ticket(tmp_path):
    assert voice_queue.heartbeat_ticket(tmp_path, "000000000000001-1") is False


def test_ticket_exists(tmp_path):
    assert voice_queue.ticket_exists(tmp_path, "0000000000000001-1") is False
    name = voice_queue.create_ticket(tmp_path, "p", "v")
    assert voice_queue.ticket_exists(tmp_path, name) is True


def test_fifo_order_and_head(tmp_path):
    # Foreign ticket older than ours (fake but live: our own pid+st)
    qdir = tmp_path / "queue"
    qdir.mkdir()
    (qdir / "000000000000001-77777.json").write_text(json.dumps(
        {"pid": os.getpid(), "start_time": _my_st(), "project": "older",
         "voice": "v", "created": "now", "last_seen": time.time()}))
    voice_queue.create_ticket(tmp_path, "me", "v")
    tickets = voice_queue.list_tickets(tmp_path)
    assert tickets[0][1]["project"] == "older"
    assert voice_queue.head_is_me(tmp_path) is True  # foreign ticket's pid is faked to ours so head_is_me returns True
    names = [t[0] for t in tickets]
    assert names == sorted(names)


# ---------- floor ----------

def _fake_floor(tmp_path, pid, start_time, last_activity):
    voice_queue._write_json_atomic(tmp_path / "floor.json", {
        "pid": pid, "start_time": start_time, "project": "other",
        "voice": "v", "acquired": "now", "last_activity": last_activity})


def test_claim_empty_floor(tmp_path):
    assert voice_queue.try_claim_floor(tmp_path, "p", "v") is True
    floor = voice_queue._read_json(tmp_path / "floor.json")
    assert floor["pid"] == os.getpid()
    assert floor["start_time"] == _my_st()
    # No leftover temp/stale files
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "floor.json"]
    assert leftovers in ([], ["queue"])


def test_claim_blocked_by_live_floor(tmp_path):
    # Use a real other live process: spawn a sleeper.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _fake_floor(tmp_path, proc.pid,
                    voice_queue.process_start_time(proc.pid), time.time())
        assert voice_queue.try_claim_floor(tmp_path, "p", "v") is False
    finally:
        proc.kill()
        proc.wait()


def test_claim_dead_pid_floor(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = proc.pid
    st = voice_queue.process_start_time(pid)
    proc.wait()
    _fake_floor(tmp_path, pid, st if st is not None else -1, time.time())
    assert voice_queue.try_claim_floor(tmp_path, "p", "v") is True


def test_claim_grace_expired_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_queue, "QUEUE_GRACE", 0.1)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _fake_floor(tmp_path, proc.pid,
                    voice_queue.process_start_time(proc.pid),
                    time.time() - 1.0)  # last_activity 1s ago > 0.1s grace
        assert voice_queue.try_claim_floor(tmp_path, "p", "v") is True
    finally:
        proc.kill()
        proc.wait()


def test_floor_is_mine_and_release(tmp_path):
    assert voice_queue.floor_is_mine(tmp_path) is False
    voice_queue.try_claim_floor(tmp_path, "p", "v")
    assert voice_queue.floor_is_mine(tmp_path) is True
    voice_queue.release_floor(tmp_path)
    assert voice_queue.floor_is_mine(tmp_path) is False
    assert not (tmp_path / "floor.json").exists()


def test_heartbeat_floor_conditional(tmp_path):
    voice_queue.try_claim_floor(tmp_path, "p", "v")
    before = voice_queue._read_json(tmp_path / "floor.json")["last_activity"]
    time.sleep(0.05)
    assert voice_queue.heartbeat_floor(tmp_path) is True
    after = voice_queue._read_json(tmp_path / "floor.json")["last_activity"]
    assert after > before
    # Floor stolen (someone else's pid): heartbeat must refuse (demotion)
    _fake_floor(tmp_path, 999999999, 1, time.time())
    assert voice_queue.heartbeat_floor(tmp_path) is False
    # And must NOT have overwritten the thief's floor
    assert voice_queue._read_json(tmp_path / "floor.json")["pid"] == 999999999


def test_release_only_own_floor(tmp_path):
    _fake_floor(tmp_path, 999999999, 1, time.time())
    voice_queue.release_floor(tmp_path)  # not ours -> no-op
    assert (tmp_path / "floor.json").exists()


def test_release_puts_back_stolen_floor(tmp_path):
    # We think we hold it, but a thief replaced floor.json before our release
    _fake_floor(tmp_path, 999999999, 1, time.time())
    voice_queue.release_floor(tmp_path)
    floor = voice_queue._read_json(tmp_path / "floor.json")
    assert floor is not None and floor["pid"] == 999999999


def test_claim_contention_exactly_one_winner(tmp_path):
    """8 subprocesses race to claim a free floor: exactly one succeeds.
    The winner stays alive until killed — a dead winner's floor would be
    legitimately re-claimable via the dead-pid path, breaking the count."""
    helper = Path(__file__).parent / "queue_helper.py"
    procs = [subprocess.Popen(
        [sys.executable, str(helper), "claim", str(tmp_path)],
        stdout=subprocess.PIPE) for _ in range(8)]
    try:
        results = [p.stdout.readline().decode().strip() for p in procs]
    finally:
        for p in procs:
            p.kill()
            p.wait()
    assert results.count("WON") == 1
    assert results.count("LOST") == 7


def test_toctou_heartbeating_holder_not_stolen(tmp_path, monkeypatch):
    """A live holder that heartbeats faster than grace cannot be stolen;
    once it stops, the claim succeeds."""
    monkeypatch.setattr(voice_queue, "QUEUE_GRACE", 0.3)
    helper = Path(__file__).parent / "queue_helper.py"
    holder = subprocess.Popen(
        [sys.executable, str(helper), "hold", str(tmp_path), "1.0", "0.05"],
        env={**os.environ, "VOICEMODE_QUEUE_GRACE": "0.3"})
    try:
        time.sleep(0.2)  # holder has claimed and is heartbeating
        deadline = time.monotonic() + 0.8
        stolen = False
        while time.monotonic() < deadline:
            if voice_queue.try_claim_floor(tmp_path, "thief", "v"):
                stolen = True
                break
            time.sleep(0.05)
        assert stolen is False, "claimed a floor whose holder was heartbeating"
        holder.wait(timeout=5)  # holder releases after 1.0s
        assert voice_queue.try_claim_floor(tmp_path, "thief", "v") is True
    finally:
        holder.kill()


# ---------- QueueSession ----------
import asyncio


def test_voice_short_name():
    assert voice_queue.voice_short_name("af_bella") == "Bella"
    assert voice_queue.voice_short_name("p_de_thorsten") == "Thorsten"
    assert voice_queue.voice_short_name("nova") == "Nova"
    assert voice_queue.voice_short_name(None) == "default voice"


def test_session_project_env_override(monkeypatch):
    monkeypatch.setenv("VOICEMODE_SESSION_NAME", "my-session")
    assert voice_queue.session_project() == "my-session"
    monkeypatch.delenv("VOICEMODE_SESSION_NAME")
    assert voice_queue.session_project() == Path(os.getcwd()).name


def test_acquire_instant_when_free(tmp_path):
    s = voice_queue.QueueSession(project="p", voice="af_bella", base=tmp_path)
    r = asyncio.run(s.acquire())
    assert r.status == "acquired"
    assert r.waited is False           # no intro
    assert voice_queue.floor_is_mine(tmp_path)
    assert voice_queue.list_tickets(tmp_path) == []  # ticket consumed


def test_burst_continuation_and_end_burst(tmp_path):
    s = voice_queue.QueueSession(project="p", voice="af_bella", base=tmp_path)

    async def flow():
        r1 = await s.acquire()
        await s.finish(end_burst=False)
        assert voice_queue.floor_is_mine(tmp_path)   # floor kept across calls
        r2 = await s.acquire()                        # burst continuation
        assert r2.status == "acquired" and r2.waited is False
        await s.finish(end_burst=True)
    asyncio.run(flow())
    assert not (tmp_path / "floor.json").exists()     # floor released


def test_acquire_queued_when_floor_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_queue, "WAIT_SLICE", 0.3)
    monkeypatch.setattr(voice_queue, "CHECK_INTERVAL", 0.05)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _fake_floor(tmp_path, proc.pid,
                    voice_queue.process_start_time(proc.pid), time.time())
        s = voice_queue.QueueSession(project="p", voice="af_bella", base=tmp_path)
        r = asyncio.run(s.acquire())
        assert r.status == "queued"
        assert r.ticket is not None
        assert "QUEUED" in r.queued_message
        assert f'ticket="{r.ticket}"' in r.queued_message
        assert "Do NOT" in r.queued_message
        # Ticket persists for the re-call
        assert voice_queue.ticket_exists(tmp_path, r.ticket)
    finally:
        proc.kill()
        proc.wait()


def test_recall_with_ticket_preserves_position_and_intro(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_queue, "WAIT_SLICE", 0.3)
    monkeypatch.setattr(voice_queue, "CHECK_INTERVAL", 0.05)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    s = voice_queue.QueueSession(project="p", voice="af_bella", base=tmp_path)
    try:
        _fake_floor(tmp_path, proc.pid,
                    voice_queue.process_start_time(proc.pid), time.time())
        r1 = asyncio.run(s.acquire())
        assert r1.status == "queued"
    finally:
        proc.kill()
        proc.wait()
    # Floor holder died; re-call with the ticket must acquire WITH intro
    r2 = asyncio.run(s.acquire(ticket=r1.ticket))
    assert r2.status == "acquired"
    assert r2.waited is True
    assert s.intro == "This is p, Bella —"


def test_recall_with_vanished_ticket_requeues(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_queue, "WAIT_SLICE", 0.2)
    monkeypatch.setattr(voice_queue, "CHECK_INTERVAL", 0.05)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _fake_floor(tmp_path, proc.pid,
                    voice_queue.process_start_time(proc.pid), time.time())
        s = voice_queue.QueueSession(project="p", voice="v", base=tmp_path)
        r = asyncio.run(s.acquire(ticket="0000000000000001-424242"))
        assert r.status == "queued"
        assert r.ticket != "0000000000000001-424242"   # fresh ticket
        assert "re-queued" in r.queued_message
    finally:
        proc.kill()
        proc.wait()


def test_heartbeat_task_keeps_floor_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_queue, "QUEUE_GRACE", 0.3)
    monkeypatch.setattr(voice_queue, "HEARTBEAT_INTERVAL", 0.05)
    s = voice_queue.QueueSession(project="p", voice="v", base=tmp_path)

    async def flow():
        await s.acquire()
        s.start_heartbeat()
        await asyncio.sleep(0.6)  # > grace; heartbeats must keep us live
        data = voice_queue._read_json(tmp_path / "floor.json")
        assert voice_queue.floor_is_live(data)
        await s.finish(end_burst=True)
    asyncio.run(flow())


def test_finish_after_queued_touches_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_queue, "WAIT_SLICE", 0.2)
    monkeypatch.setattr(voice_queue, "CHECK_INTERVAL", 0.05)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _fake_floor(tmp_path, proc.pid,
                    voice_queue.process_start_time(proc.pid), time.time())
        before = voice_queue._read_json(tmp_path / "floor.json")
        s = voice_queue.QueueSession(project="p", voice="v", base=tmp_path)

        async def flow():
            r = await s.acquire()
            assert r.status == "queued"
            await s.finish(end_burst=True)   # must not touch the holder's floor
        asyncio.run(flow())
        after = voice_queue._read_json(tmp_path / "floor.json")
        assert after == before
    finally:
        proc.kill()
        proc.wait()


# ---------- status ----------

def test_print_status(tmp_path, capsys):
    voice_queue.print_status(tmp_path)
    out = capsys.readouterr().out
    assert "Floor: free" in out
    assert "Queue: empty" in out

    voice_queue.try_claim_floor(tmp_path, "projA", "af_bella")
    qdir = tmp_path / "queue"
    qdir.mkdir(exist_ok=True)
    (qdir / "0000000000000001-77777.json").write_text(json.dumps(
        {"pid": os.getpid(), "start_time": _my_st(), "project": "projB",
         "voice": "bm_daniel", "created": "now", "last_seen": time.time()}))
    voice_queue.print_status(tmp_path)
    out = capsys.readouterr().out
    assert "projA/af_bella" in out
    assert "projB/bm_daniel" in out


# ---------- logging ----------

def test_log_writes_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_queue, "QUEUE_LOG", True)
    voice_queue._log("unit_event", tmp_path, project="p", voice="v", extra=1)
    logfile = tmp_path / "logs" / "queue.log"
    assert logfile.exists()
    rec = json.loads(logfile.read_text().strip())
    assert rec["event"] == "unit_event"
    assert rec["pid"] == os.getpid()
    assert rec["project"] == "p" and rec["voice"] == "v" and rec["extra"] == 1
    assert "ts" in rec


def test_log_disabled_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_queue, "QUEUE_LOG", False)
    voice_queue._log("unit_event", tmp_path, project="p")
    assert not (tmp_path / "logs").exists()


def test_acquire_emits_log_events(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_queue, "QUEUE_LOG", True)
    s = voice_queue.QueueSession(project="p", voice="af_bella", base=tmp_path)

    async def flow():
        await s.acquire()
        await s.finish(end_burst=True)
    asyncio.run(flow())
    events = [json.loads(l)["event"]
              for l in (tmp_path / "logs" / "queue.log").read_text().splitlines()]
    assert "acquire_call" in events
    assert "acquired_floor" in events
    assert "released" in events


# ---------- Option B: max-hold rotation + grace-expiry yield ----------

def _live_foreign_waiter(tmp_path, proc, project="waiter"):
    """Write a queue ticket owned by a live foreign process (sorts to head)."""
    qdir = tmp_path / "queue"
    qdir.mkdir(exist_ok=True)
    (qdir / f"0000000000000001-{proc.pid}.json").write_text(json.dumps(
        {"pid": proc.pid, "start_time": voice_queue.process_start_time(proc.pid),
         "project": project, "voice": "v", "created": "now",
         "last_seen": time.time()}))


def test_burst_yields_to_waiter_after_max_hold(tmp_path, monkeypatch):
    # MAX_HOLD=0 => any continuous hold has exhausted its budget, so a waiting
    # session preempts at the next exchange boundary (the monopoly fix).
    monkeypatch.setattr(voice_queue, "MAX_HOLD", 0.0)
    monkeypatch.setattr(voice_queue, "WAIT_SLICE", 0.2)
    monkeypatch.setattr(voice_queue, "CHECK_INTERVAL", 0.05)
    s = voice_queue.QueueSession(project="p", voice="af_bella", base=tmp_path)
    assert asyncio.run(s.acquire()).status == "acquired"
    assert voice_queue.floor_is_mine(tmp_path)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _live_foreign_waiter(tmp_path, proc)
        r = asyncio.run(s.acquire())                 # next boundary: must yield
        assert r.status == "queued"
        assert r.ticket is not None
        assert not voice_queue.floor_is_mine(tmp_path)   # floor freed for waiter
    finally:
        proc.kill()
        proc.wait()


def test_burst_continues_past_max_hold_when_no_waiters(tmp_path, monkeypatch):
    # Even with the budget exhausted, a holder keeps the floor if nobody waits.
    monkeypatch.setattr(voice_queue, "MAX_HOLD", 0.0)
    s = voice_queue.QueueSession(project="p", voice="af_bella", base=tmp_path)

    async def flow():
        await s.acquire()
        await s.finish(end_burst=False)
        r = await s.acquire()                        # no waiters -> keep floor
        assert r.status == "acquired" and r.waited is False
        assert voice_queue.floor_is_mine(tmp_path)
        await s.finish(end_burst=True)
    asyncio.run(flow())
    assert not (tmp_path / "floor.json").exists()


def test_burst_continues_within_budget_even_with_waiters(tmp_path, monkeypatch):
    # Inside the hold budget, an active conversation is NOT chopped up.
    monkeypatch.setattr(voice_queue, "MAX_HOLD", 100.0)
    s = voice_queue.QueueSession(project="p", voice="af_bella", base=tmp_path)
    assert asyncio.run(s.acquire()).status == "acquired"
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _live_foreign_waiter(tmp_path, proc)
        r = asyncio.run(s.acquire())                 # within budget -> keep floor
        assert r.status == "acquired" and r.waited is False
        assert voice_queue.floor_is_mine(tmp_path)
    finally:
        proc.kill()
        proc.wait()


def test_grace_expired_holder_yields_to_waiter(tmp_path, monkeypatch):
    # Bug #2: a holder whose grace has expired must NOT self-refresh its stale
    # floor when others are waiting — it yields (independent of MAX_HOLD).
    # This is the *inter-exchange* case: the holder paused (finish leaves the
    # exchange -> in_exchange False) and then went quiet past grace. A holder
    # still mid-exchange is never stale (see in_exchange tests).
    monkeypatch.setattr(voice_queue, "QUEUE_GRACE", 0.1)
    monkeypatch.setattr(voice_queue, "MAX_HOLD", 100.0)
    monkeypatch.setattr(voice_queue, "WAIT_SLICE", 0.2)
    monkeypatch.setattr(voice_queue, "CHECK_INTERVAL", 0.05)
    s = voice_queue.QueueSession(project="p", voice="af_bella", base=tmp_path)
    assert asyncio.run(s.acquire()).status == "acquired"
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _live_foreign_waiter(tmp_path, proc)
        asyncio.run(s.finish(end_burst=False))       # pause -> leave the exchange
        time.sleep(0.2)                              # then go quiet past grace
        r = asyncio.run(s.acquire())                 # stale + waiter -> yield
        assert r.status == "queued"
        assert not voice_queue.floor_is_mine(tmp_path)
    finally:
        proc.kill()
        proc.wait()


def test_grace_expired_holder_keeps_floor_when_alone(tmp_path, monkeypatch):
    # Stale floor but nobody waiting: harmless to resume the burst.
    monkeypatch.setattr(voice_queue, "QUEUE_GRACE", 0.1)
    s = voice_queue.QueueSession(project="p", voice="af_bella", base=tmp_path)
    assert asyncio.run(s.acquire()).status == "acquired"
    time.sleep(0.2)                                  # grace expires, no waiters
    r = asyncio.run(s.acquire())
    assert r.status == "acquired" and r.waited is False
    assert voice_queue.floor_is_mine(tmp_path)


def test_pause_hands_off_to_waiter_under_fifo(tmp_path, monkeypatch):
    # The away-doing-work fix: finish(end_burst=False) RELEASES the floor when
    # another session is waiting (MAX_HOLD=0), so a session that pauses to run a
    # long tool call hands the mic off immediately instead of holding through the
    # full grace window.
    monkeypatch.setattr(voice_queue, "MAX_HOLD", 0.0)
    s = voice_queue.QueueSession(project="p", voice="af_bella", base=tmp_path)
    assert asyncio.run(s.acquire()).status == "acquired"
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _live_foreign_waiter(tmp_path, proc)
        asyncio.run(s.finish(end_burst=False))
        assert not (tmp_path / "floor.json").exists()   # released at the pause
    finally:
        proc.kill()
        proc.wait()


def test_pause_keeps_floor_when_alone(tmp_path, monkeypatch):
    # No waiters: a pause keeps the floor (grace covers brief thinking gaps), so
    # a solo conversation is never interrupted.
    monkeypatch.setattr(voice_queue, "MAX_HOLD", 0.0)
    s = voice_queue.QueueSession(project="p", voice="af_bella", base=tmp_path)
    assert asyncio.run(s.acquire()).status == "acquired"
    asyncio.run(s.finish(end_burst=False))
    assert voice_queue.floor_is_mine(tmp_path)


def test_pause_keeps_floor_within_budget(tmp_path, monkeypatch):
    # With a non-zero budget, an active burst is not handed off at every pause.
    monkeypatch.setattr(voice_queue, "MAX_HOLD", 100.0)
    s = voice_queue.QueueSession(project="p", voice="af_bella", base=tmp_path)
    assert asyncio.run(s.acquire()).status == "acquired"
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _live_foreign_waiter(tmp_path, proc)
        asyncio.run(s.finish(end_burst=False))
        assert voice_queue.floor_is_mine(tmp_path)       # within budget -> keep
    finally:
        proc.kill()
        proc.wait()


# ---------- in_exchange phase flag (2026-06-10 barge-in fix) ----------
# Root cause: floor_is_live required last_activity within QUEUE_GRACE, refreshed
# by a call-scoped asyncio heartbeat. During a long TTS turn the heartbeat is
# starved (blocking sd.OutputStream.write on the event loop), last_activity
# freezes, the floor goes stale, and a FIFO waiter steals it mid-speech.
# Fix: a holder actively in an exchange (in_exchange=True) is live as long as its
# process is alive, regardless of last_activity age. Grace only judges the
# inter-exchange gap (in_exchange=False).

def test_in_exchange_holder_not_stale_despite_frozen_activity(tmp_path, monkeypatch):
    """A holder mid-exchange stays live even when last_activity is far older than
    grace (heartbeat starved during a long TTS turn). Reproduces the barge-in."""
    monkeypatch.setattr(voice_queue, "QUEUE_GRACE", 0.1)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        st = voice_queue.process_start_time(proc.pid)
        voice_queue._write_json_atomic(tmp_path / "floor.json", {
            "pid": proc.pid, "start_time": st, "project": "p", "voice": "v",
            "acquired": "now", "last_activity": time.time() - 100.0,
            "in_exchange": True})
        floor = voice_queue._read_json(tmp_path / "floor.json")
        assert voice_queue.floor_is_live(floor) is True
        assert voice_queue.try_claim_floor(tmp_path, "thief", "v") is False
    finally:
        proc.kill()
        proc.wait()


def test_in_exchange_false_falls_back_to_grace(tmp_path, monkeypatch):
    """Between exchanges (in_exchange=False), a holder idle past grace is stale
    and claimable — preserves the inter-exchange handoff."""
    monkeypatch.setattr(voice_queue, "QUEUE_GRACE", 0.1)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        st = voice_queue.process_start_time(proc.pid)
        voice_queue._write_json_atomic(tmp_path / "floor.json", {
            "pid": proc.pid, "start_time": st, "project": "p", "voice": "v",
            "acquired": "now", "last_activity": time.time() - 1.0,
            "in_exchange": False})
        assert voice_queue.try_claim_floor(tmp_path, "thief", "v") is True
    finally:
        proc.kill()
        proc.wait()


def test_in_exchange_dead_holder_still_claimable(tmp_path):
    """in_exchange=True must NOT protect a dead holder — process death frees it."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = proc.pid
    st = voice_queue.process_start_time(pid)
    proc.wait()
    voice_queue._write_json_atomic(tmp_path / "floor.json", {
        "pid": pid, "start_time": st if st is not None else -1, "project": "p",
        "voice": "v", "acquired": "now", "last_activity": time.time(),
        "in_exchange": True})
    assert voice_queue.try_claim_floor(tmp_path, "thief", "v") is True


def test_claim_marks_in_exchange(tmp_path):
    """Claiming the floor means a converse exchange is starting."""
    assert voice_queue.try_claim_floor(tmp_path, "p", "v") is True
    floor = voice_queue._read_json(tmp_path / "floor.json")
    assert floor.get("in_exchange") is True


def test_heartbeat_floor_can_set_in_exchange(tmp_path):
    """heartbeat_floor(in_exchange=...) toggles the phase flag while refreshing."""
    voice_queue.try_claim_floor(tmp_path, "p", "v")
    assert voice_queue._read_json(tmp_path / "floor.json")["in_exchange"] is True
    assert voice_queue.heartbeat_floor(tmp_path, in_exchange=False) is True
    assert voice_queue._read_json(tmp_path / "floor.json")["in_exchange"] is False
    assert voice_queue.heartbeat_floor(tmp_path, in_exchange=True) is True
    assert voice_queue._read_json(tmp_path / "floor.json")["in_exchange"] is True


def test_finish_pause_clears_in_exchange(tmp_path):
    """A normal pause (finish without end_burst) leaves the exchange so the
    inter-exchange grace window applies until the next call."""
    s = voice_queue.QueueSession(project="p", voice="v", base=tmp_path)

    async def flow():
        await s.acquire()
        assert voice_queue._read_json(tmp_path / "floor.json")["in_exchange"] is True
        await s.finish(end_burst=False)
    asyncio.run(flow())
    floor = voice_queue._read_json(tmp_path / "floor.json")
    assert floor is not None and floor["in_exchange"] is False


def test_finish_pause_demoted_marks_not_acquired(tmp_path):
    """If the floor was stolen mid-exchange (residual race), finish must notice
    the demotion, drop _acquired, and not clobber the thief's floor."""
    s = voice_queue.QueueSession(project="p", voice="v", base=tmp_path)

    async def flow():
        await s.acquire()
        _fake_floor(tmp_path, 999999999, 1, time.time())  # thief steals it
        await s.finish(end_burst=False)
    asyncio.run(flow())
    assert s._acquired is False
    assert voice_queue._read_json(tmp_path / "floor.json")["pid"] == 999999999


# ---------- in_exchange freshness ceiling (wedged-holder safety) ----------
# in_exchange keeps the floor live during a call even when last_activity is
# stale (a long TTS blocks the event loop so the heartbeat can't refresh). But
# it must NOT be unbounded: a live-but-WEDGED process (loop stuck forever) would
# otherwise hold the floor permanently and starve every waiter. So in_exchange is
# honored only while last_activity is within IN_EXCHANGE_MAX; beyond that the
# holder is treated as wedged and the floor becomes reclaimable.

def _in_exchange_floor(tmp_path, pid, st, last_activity):
    voice_queue._write_json_atomic(tmp_path / "floor.json", {
        "pid": pid, "start_time": st, "project": "p", "voice": "v",
        "acquired": "now", "last_activity": last_activity, "in_exchange": True})


def test_in_exchange_within_ceiling_still_live(tmp_path, monkeypatch):
    """A long-but-progressing TTS turn (last_activity stale but under the
    ceiling) stays live — no barge-in."""
    monkeypatch.setattr(voice_queue, "QUEUE_GRACE", 0.1)
    monkeypatch.setattr(voice_queue, "IN_EXCHANGE_MAX", 5.0)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        st = voice_queue.process_start_time(proc.pid)
        _in_exchange_floor(tmp_path, proc.pid, st, time.time() - 2.0)  # 2s < 5s ceiling
        assert voice_queue.floor_is_live(voice_queue._read_json(tmp_path / "floor.json")) is True
        assert voice_queue.try_claim_floor(tmp_path, "thief", "v") is False
    finally:
        proc.kill(); proc.wait()


def test_in_exchange_past_ceiling_is_reclaimable(tmp_path, monkeypatch):
    """A wedged-but-alive holder (last_activity older than IN_EXCHANGE_MAX) is
    NOT live — the floor can be reclaimed so waiters aren't starved forever."""
    monkeypatch.setattr(voice_queue, "QUEUE_GRACE", 0.1)
    monkeypatch.setattr(voice_queue, "IN_EXCHANGE_MAX", 5.0)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        st = voice_queue.process_start_time(proc.pid)
        _in_exchange_floor(tmp_path, proc.pid, st, time.time() - 10.0)  # 10s > 5s ceiling
        assert voice_queue.floor_is_live(voice_queue._read_json(tmp_path / "floor.json")) is False
        assert voice_queue.try_claim_floor(tmp_path, "thief", "v") is True
    finally:
        proc.kill(); proc.wait()


# ---------- no-speech timeout under contention ----------
# When other sessions are waiting, a holder listening to a SILENT user should
# not grip the mic for the full listen window. effective_no_speech_timeout()
# returns LISTEN_CAP so the recording loop stops early — but ONLY if speech
# never started. Active speech is never truncated (that was the 2026-06-11
# truncation bug: the cap used to land on listen_duration_max, a hard ceiling,
# cutting the user off mid-sentence at ~8s). Solo conversation: no timeout.

def test_no_speech_timeout_none_without_waiters(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_queue, "LISTEN_CAP", 8.0)
    s = voice_queue.QueueSession(project="p", voice="v", base=tmp_path)
    assert s.effective_no_speech_timeout() is None   # solo: wait full window


def test_no_speech_timeout_applies_with_waiter(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_queue, "LISTEN_CAP", 8.0)
    s = voice_queue.QueueSession(project="p", voice="v", base=tmp_path)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _live_foreign_waiter(tmp_path, proc)
        assert s.effective_no_speech_timeout() == 8.0   # contended: yield if silent
    finally:
        proc.kill(); proc.wait()


def test_no_speech_timeout_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_queue, "LISTEN_CAP", 0.0)  # 0 disables
    s = voice_queue.QueueSession(project="p", voice="v", base=tmp_path)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _live_foreign_waiter(tmp_path, proc)
        assert s.effective_no_speech_timeout() is None   # disabled -> no timeout
    finally:
        proc.kill(); proc.wait()
