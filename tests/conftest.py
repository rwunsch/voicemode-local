"""Shared pytest hooks.

Tests that need the pristine voice-mode 8.12.0 sources skip when VM812_SRC is not
set ("VM812_SRC not set ..."; on Windows "VML_APPLY_SRC ... not set", which the runner
derives from VM812_SRC). A skipped patch check looks like a passed run, so:

  VOICEMODE_RELEASE_CHECK=1   each such skip becomes a failure with a clear message
  otherwise                   the skips stay, and a loud warning ends the run
"""
import os

import pytest

_NEEDS_SOURCES = ("VM812_SRC not set", "VML_APPLY_SRC / VML_APPLY_EXPECTED not set")
_skipped_for_sources = []


def _release_check() -> bool:
    return os.environ.get("VOICEMODE_RELEASE_CHECK") == "1"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if not report.skipped or not any(text in str(report.longrepr) for text in _NEEDS_SOURCES):
        return
    _skipped_for_sources.append(item.nodeid)
    if _release_check():
        report.outcome = "failed"
        report.longrepr = (
            f"{item.nodeid}: VOICEMODE_RELEASE_CHECK=1 but VM812_SRC is not set. This test "
            "checks the patches against pristine voice-mode 8.12.0 sources and must run "
            "before a release: set VM812_SRC to the folder that holds them.")


def pytest_terminal_summary(terminalreporter):
    if not _skipped_for_sources or _release_check():
        return
    count = len(_skipped_for_sources)
    terminalreporter.write_sep("!", f"WARNING: {count} test(s) skipped because VM812_SRC is not set",
                               red=True, bold=True)
    terminalreporter.write_line("The patch checks against pristine voice-mode 8.12.0 did NOT run. "
                                "Set VM812_SRC, or VOICEMODE_RELEASE_CHECK=1 to make this fail.",
                                red=True, bold=True)


@pytest.fixture(autouse=True)
def _no_real_claude_sessions(tmp_path_factory, monkeypatch):
    """session_wake reads Claude Code's session registry (CLAUDE_CONFIG_DIR, else
    ~/.claude). Point it at an empty temp dir for every test, so no test can ever
    find, let alone message, a real Claude Code session on this machine."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("no-claude")))
