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
