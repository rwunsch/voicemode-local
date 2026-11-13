#!/usr/bin/env python3
"""Bound the git call that saves transcripts (config.py get_project_path).

Upstream runs `git rev-parse --show-toplevel` with no timeout and with the MCP stdio pipe
as stdin, synchronously on the event loop (save_transcription, reached from converse when
transcripts are saved; VOICEMODE_DEBUG=true implies it). On Windows 2026-11-13 (n=1,
reported by a peer session) it hung 176 s and the conch stayed held for every chat.

This patch adds stdin=DEVNULL, timeout=5 and, on Windows, CREATE_NO_WINDOW. A timeout
falls back to os.getcwd(), which is upstream's documented fallback; it is logged once.

Idempotent (marker). Stops setup if an anchor is not found exactly once. Only config.py.
Stdlib only.
"""
import sys
from pathlib import Path

MARKER = "VML: GIT TIMEOUT"

PAIRS = [
    ("            text=True,\n"
     "            cwd=os.getcwd()\n"
     "        )\n",
     "            text=True,\n"
     "            cwd=os.getcwd(),\n"
     "            stdin=subprocess.DEVNULL,  # " + MARKER + ": never inherit the MCP stdio pipe\n"
     "            timeout=5,\n"
     "            creationflags=getattr(subprocess, \"CREATE_NO_WINDOW\", 0)\n"
     "        )\n"),
    ("        if result.returncode == 0:\n"
     "            return result.stdout.strip()\n"
     "    except Exception:\n"
     "        pass\n",
     "        if result.returncode == 0:\n"
     "            return result.stdout.strip()\n"
     "    except subprocess.TimeoutExpired:  # " + MARKER + ": fall back to the cwd, logged once\n"
     "        if not getattr(get_project_path, \"_vml_timeout_logged\", False):\n"
     "            get_project_path._vml_timeout_logged = True\n"
     "            try:\n"
     "                logger.warning(\"git rev-parse timed out after 5 s; using the working directory as the project path\")\n"
     "            except Exception:\n"
     "                pass\n"
     "    except Exception:\n"
     "        pass\n"),
]


def _read(target):
    raw = target.read_bytes()
    ending = "\r\n" if b"\r\n" in raw else "\n"
    return raw.decode("utf-8").replace("\r\n", "\n"), ending


def _write(target, text, ending):
    target.write_bytes(text.replace("\n", ending).encode("utf-8"))


def apply(target):
    source, ending = _read(target)
    if target.name != "config.py":
        print(f"[patch_git_timeout] {target}: only config.py is patched", file=sys.stderr)
        return 1
    if MARKER in source:
        if any(p[1] not in source for p in PAIRS):
            print(f"[{Path(__file__).name}] {target}: carries an older version of this patch; "
                  "it is kept. To get the new text, reinstall voice-mode and apply again "
                  "(docs/patching/README.md).", file=sys.stderr)
        return 0
    for old, _new in PAIRS:
        if source.count(old) != 1:
            print(f"[patch_git_timeout] {target}: an anchor was found {source.count(old)} times, "
                  "not 1", file=sys.stderr)
            return 1
    for old, new in PAIRS:
        source = source.replace(old, new)
    compile(source, str(target), "exec")
    _write(target, source, ending)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: patch_git_timeout.py <path to config.py>", file=sys.stderr)
        sys.exit(2)
    sys.exit(apply(Path(sys.argv[1])))
