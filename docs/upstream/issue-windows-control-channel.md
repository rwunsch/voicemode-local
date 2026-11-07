# DRAFT (not filed) — issue only (no PR unless the maintainer engages)

**Title:** Control channel can't run on native Windows (AF_UNIX), and enabling it there can fail the converse call

**Body:**

The control channel (`VOICEMODE_CONTROL_CHANNEL_ENABLED=true`: pause, resume, stop,
skip_forward from another process) listens on a Unix domain socket:
`voice_mode/control_socket.py:317` creates `socket.socket(socket.AF_UNIX, ...)`.

Native Windows Python has no `socket.AF_UNIX` (checked on CPython 3.12.10 and 3.14.7 for
Windows), so on Windows the channel has no transport at all: push-to-talk, a Stream Deck or a
status window cannot reach a running `converse`.

**A second, smaller problem (found by reading v9.0.2, not run):** with the flag on, on Windows,
`socket.AF_UNIX` raises `AttributeError`, but `start_control_listener` only catches `OSError`
around the bind (`control_socket.py` ~571-573), and `_control_listener_scope` in
`tools/converse.py` (~1784) calls it outside its `try`. So the error would escape and fail the
converse call instead of the documented "bind failures are non-fatal". Catching
`AttributeError` there (or checking `hasattr(socket, "AF_UNIX")`) would keep Windows users who
copy a shared config working.

**What we use downstream, in case it helps:** a named pipe per base directory
(`\\.\pipe\voicemode-control-<hash of the socket path>`), created with an owner-only DACL,
`FILE_FLAG_FIRST_PIPE_INSTANCE` and `PIPE_REJECT_REMOTE_CLIENTS`, with the same wire protocol
as the Unix socket; clients check the pipe's owner SID. Running on Windows 11 with push-to-talk
and a status window. Happy to turn that into a PR if a Windows transport is wanted.
