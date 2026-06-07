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
    assert voice_queue.head_is_me(tmp_path) is True  # head pid == ours (faked)
    names = [t[0] for t in tickets]
    assert names == sorted(names)
