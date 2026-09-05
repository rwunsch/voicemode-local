"""Does 8.12.0 still need patch_listen_stall.py / patch_listen_overrun.py?

8.11.0 claims "a dead-stream stall backstop in the recording loop"; VM-2015
claims recording is now cooperatively cancellable (0.4s vs 17.5s).

8.7.1 bugs:
  stall   - recording_duration only advances when a chunk is dequeued, so a
            starved capture callback freezes it and the loop never exits.
  overrun - `while recording_duration < max_duration` hard-cuts a user who is
            still speaking at the cap.
"""
import re

import pytest


@pytest.fixture(scope="module")
def loop_src(request):
    src = request.getfixturevalue("converse_src")
    m = re.search(
        r"(async )?def record_audio_with_silence_detection.*?(?=\n(async )?def |\nclass )",
        src, re.S,
    )
    if not m:
        pytest.fail("record_audio_with_silence_detection not found - upstream restructured")
    return m.group(0)


def test_recording_loop_has_a_wallclock_backstop(loop_src):
    """A real stall backstop must consult wall-clock, not only dequeued chunks."""
    assert re.search(r"time\.(monotonic|time)\(\)|monotonic\(\)", loop_src), (
        "no wall-clock reference in the recording loop - patch_listen_stall.py STILL NEEDED"
    )


@pytest.mark.xfail(
    strict=True,
    reason="LIVE UPSTREAM BUG as of 8.12.0 -- converse.py:1467 still caps an "
           "active recording at max_duration. We patch it (patch_listen_overrun) "
           "and have drafted the fix as docs/upstream/pr-listen-overrun.md. "
           "strict=True so this turns RED the day upstream fixes it, which is "
           "when we should retire our patch.",
)
def test_speech_not_truncated_by_bare_max_duration_cap(loop_src):
    r"""The overrun fix: a bare `recording_duration < max_duration` bound is the bug.

    NOTE the `while (` form -- 8.12.0 wraps the condition in parentheses, so an
    anchored `while\s+recording_duration` regex passes for the WRONG reason.
    Match the bound wherever it appears in the loop condition.
    """
    loop_head = re.search(r"while\s*\(?(.*?)\):\n", loop_src, re.S)
    assert loop_head, "could not isolate the recording loop condition"
    cond = loop_head.group(1)
    naive = re.search(r"recording_duration\s*<\s*max_duration", cond)
    assert not naive, (
        "recording loop condition is still bounded by `recording_duration < max_duration` "
        f"(condition: {cond.strip()[:80]!r}) - a user still speaking at the cap is "
        "truncated mid-word. patch_listen_overrun.py STILL NEEDED"
    )
