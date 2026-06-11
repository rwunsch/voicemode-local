"""Tests for voice_queue.session_project() session-name resolution."""
import os
import sys
import time
from pathlib import Path

import pytest

PATCHES_DIR = Path(__file__).parent.parent / "patches"
sys.path.insert(0, str(PATCHES_DIR))

import voice_queue  # noqa: E402

SID = "test-session-id-1234"


def _write_label(base: Path, sid: str, text: str) -> Path:
    d = base / voice_queue.SESSION_NAMES_DIR
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.txt"
    f.write_text(text)
    return f


def test_env_var_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEMODE_SESSION_NAME", "  from-env  ")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    _write_label(tmp_path, SID, "from-file")
    assert voice_queue.session_project(tmp_path) == "from-env"


def test_file_used_when_no_env(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICEMODE_SESSION_NAME", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    _write_label(tmp_path, SID, "  queue-naming\n")
    assert voice_queue.session_project(tmp_path) == "queue-naming"


def test_folder_name_when_no_env_no_file(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICEMODE_SESSION_NAME", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.chdir(tmp_path)
    assert voice_queue.session_project(tmp_path) == tmp_path.name


def test_empty_file_falls_back_to_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICEMODE_SESSION_NAME", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.chdir(tmp_path)
    _write_label(tmp_path, SID, "   \n")
    assert voice_queue.session_project(tmp_path) == tmp_path.name


def test_no_session_id_uses_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICEMODE_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    _write_label(tmp_path, SID, "ignored")
    assert voice_queue.session_project(tmp_path) == tmp_path.name


def test_gc_removes_old_label_keeps_fresh(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICEMODE_SESSION_NAME", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    old = _write_label(tmp_path, "old-sid", "old")
    fresh = _write_label(tmp_path, SID, "fresh")
    past = time.time() - voice_queue.SESSION_NAME_MAX_AGE - 100
    os.utime(old, (past, past))
    assert voice_queue.session_project(tmp_path) == "fresh"
    assert not old.exists()
    assert fresh.exists()
