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
