# Comment for upstream #524 — independent corroboration of the Windows binary lookup fixes

**Target:** [`mbailey/voicemode#524`](https://github.com/mbailey/voicemode/pulls/524) (open, by
`stoopkid713`, since 2026-08-18).
**Type:** supporting evidence on someone else's open PR. **Not** a rival PR, and not a new issue.
**Submit as:** `gh auth switch --user rwunsch` first.
**Status:** draft, NOT submitted — awaiting Robert's go-ahead.

## Why this and not a new issue

We hit two Windows defects in `find_whisper_server()` independently, before knowing #524 existed:

1. Every candidate path is spelled `whisper-server`, with no `.exe`, so `path.exists()` is False on
   Windows for a correctly installed binary.
2. The PATH fallback calls `subprocess.run(["which", "whisper-server"])`. There is no `which` on
   Windows, so that raises `FileNotFoundError` — uncaught, so it crashes rather than returning
   `None`.

**#524 already fixes both**, with `platform.system()` for the binary name and `shutil.which()` for
the PATH lookup. So the useful contribution is evidence that the bugs are real and reproduce on a
clean machine, not another patch. This repo's own rule: support the existing PR before opening a
rival (`README.md`, strategy rule 5).

---

## Draft comment

Independent confirmation that both halves of this are real, from a separate Windows 11 machine and
found before we knew this PR existed.

**The `.exe` half.** Installing a prebuilt `whisper-server.exe` at the first path
`find_whisper_server()` checks does not get found on stock 8.12.0, because every candidate is
spelled without the extension.

**The `which` half.** On Windows `subprocess.run(["which", ...])` raises `FileNotFoundError` rather
than returning non-zero, so the PATH fallback does not degrade — it propagates. `shutil.which()`, as
used here, is the right fix and also respects `PATHEXT`.

One extra data point that may be useful for the install path this PR touches: **whisper.cpp ships
prebuilt Windows binaries**, so the Windows install does not have to build from source at all.
`whisper-bin-x64.zip` on the build tags (e.g. `b5130`) is **8 MB** and contains `whisper-server.exe`
plus the `ggml-cpu-*.dll` set. That matters because building whisper.cpp on Windows needs cmake and
MSVC, which most users will not have — the same class of problem as #541.

For what it is worth, there is a workaround on stock 8.12.0 that needs no patch: copy the binary to
the expected path *without* the `.exe` extension. PowerShell refuses to execute an extensionless
file, but voice-mode launches it through Python's `subprocess`, which calls `CreateProcess` directly
and runs it happily. We are using that while this PR is open — it is a workaround, not an argument
against merging, and the `shutil.which()` fix is needed regardless.

Environment: Windows 11 native (not WSL), Python 3.12.10, voice-mode 8.12.0, whisper.cpp `b5130`.
