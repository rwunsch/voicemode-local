#!/usr/bin/env python3
"""Apply voicemode-local's patches to an installed voice_mode package.

What runs, and in which order, is patches/manifest.json:

  unshare  files given a private copy first when they are hard links
  steps    in order: {"patch": script, "targets": [...]} runs
           `python <script> <voice_mode>/<target>` once per target;
           {"copy": module, "to": path} installs a module into the package.

It replaces the logic that lived in apply.sh, which now only calls this, and was
checked to give byte-identical files (tests/test_apply_py.py). Needs only the
standard library, so it runs under any Python that can run voice-mode, on Linux,
macOS and native Windows.

Stops at the first failure, with that script's exit code, before anything later
in the list runs: a copied module must never arrive without the patch that
expects it, or the other way round. A file the manifest lists but that does not
exist is an error, not a skip.

Usage:
  apply.py [venv-path]               venv-path defaults to the .venv beside patches/
  apply.py --package-dir <voice_mode-dir>
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.json"


def find_package(venv: Path):
    """voice_mode inside a POSIX (lib/pythonX.Y) or Windows (Lib) venv, else None."""
    for d in sorted((Path(venv) / "lib").glob("python*/site-packages/voice_mode")):
        if d.is_dir():
            return d
    win = Path(venv) / "Lib" / "site-packages" / "voice_mode"
    return win if win.is_dir() else None


def load_manifest(path: Path = MANIFEST) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _say(out, line: str) -> None:
    print(line, file=out, flush=True)


def unshare(path: Path, out) -> None:
    """Give a hard-linked file its own copy, so patching it cannot write through.

    `uv pip install` hard-links site-packages from ~/.cache/uv, shared with the
    cache entry and every other venv built from it; patching in place once made a
    pristine venv report "already patched" (2026-09-05). Copy then rename, keeping
    mode and times, as apply.sh's `cp -p` + `mv -f` did.
    """
    if path.is_file() and os.stat(path).st_nlink > 1:
        tmp = path.with_name(path.name + ".vmltmp")
        shutil.copy2(path, tmp)
        os.replace(tmp, path)
        _say(out, f"[patches] unshared hardlink: {path}")


def run(pkg: Path, manifest: dict, python: str = sys.executable, out=None,
        patches: Path = HERE) -> int:
    out = out or sys.stdout
    pkg = Path(pkg)
    for step in manifest["steps"]:
        name = step.get("patch") or step.get("copy")
        if not name or not (patches / name).is_file():
            _say(out, f"[patches] ERROR: {name!r} is listed in the manifest but not in {patches}")
            return 1
    for rel in manifest["unshare"]:
        unshare(pkg / rel, out)
    env = dict(os.environ, PYTHONUTF8="1")
    for step in manifest["steps"]:
        if "patch" in step:
            for rel in step["targets"]:
                if out is sys.stdout:
                    sys.stdout.flush()
                r = subprocess.run([python, str(patches / step["patch"]), str(pkg / rel)],
                                   env=env, capture_output=out is not sys.stdout, text=True)
                if out is not sys.stdout:
                    out.write(r.stdout + r.stderr)
                if r.returncode != 0:
                    _say(out, f"[patches] ERROR: {step['patch']} failed on {rel} (exit {r.returncode})")
                    return r.returncode
        else:
            dest = pkg / step["to"]
            shutil.copy(patches / step["copy"], dest)
            _say(out, f"[patches] installed {step['to']}")
    _say(out, "[patches] Done. Restart Claude Code for changes to take effect.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("venv", nargs="?", default=str(HERE.parent / ".venv"))
    ap.add_argument("--package-dir", help="the voice_mode directory itself")
    ap.add_argument("--manifest", default=str(MANIFEST))
    args = ap.parse_args(argv)
    if args.package_dir:
        pkg = Path(args.package_dir)
        if not pkg.is_dir():
            print(f"[patches] ERROR: {pkg} is not a directory")
            return 1
    else:
        pkg = find_package(Path(args.venv))
        if pkg is None:
            print(f"[patches] ERROR: Could not find voice_mode in {args.venv}")
            return 1
    return run(pkg, load_manifest(Path(args.manifest)))


if __name__ == "__main__":
    sys.exit(main())
