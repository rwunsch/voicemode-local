# macOS installer (`install-mac.sh`)

> **TL;DR:** `./install-mac.sh` turns the README's manual "macOS (native, no Docker)" steps into
> one re-runnable script. **It has run once on a real Mac in a throwaway HOME (2026-11-05), but never on a fresh account.** It is tested only against stubs on
> Linux (`tests/test_install_mac.py`), which proves its control flow, questions, idempotency and
> cleanup, not that real Homebrew, uv, curl or whisper-server behave as the stubs assume.

| Field        | Value                         |
|--------------|-------------------------------|
| Status       | Run on a real Mac in a throwaway HOME, n=1; not on a fresh account |
| Created      | 2026-11-05                    |
| Project      | voicemode-local (macOS)       |

## Decisions

- Dedicated script, not a branch: the Mac setup (native, no Docker, no apt) shares almost nothing
  with `install.sh`. `install.sh` hands over to it when `uname -s` is `Darwin`.
- Voice pill and German voice are off unless asked for (`--pill`, `--german`, or an explicit yes).
  `--yes` takes the defaults, it does not say yes to everything. A pill failure only warns.
- Registers the `voicemode-mcp` wrapper, as the README and `install.ps1` do. `install.sh`
  registers the raw `voice-mode` binary, which skips service startup and the push-to-talk
  listener on a Mac.
- Downloads go to `<file>.part` and are moved into place only on success.
- Never installs Homebrew; stops with its command.

## Test on a clean Mac user account

1. Apple silicon Mac, Homebrew installed, no `~/.voicemode*`. `./install-mac.sh --yes` ends with
   no error; run it again and confirm nothing is downloaded or reinstalled.
2. `voicemode-switch health` shows Whisper (2022) and Kokoro (8880) up.
3. `claude mcp list` shows `voicemode`; `/voicemode:converse` speaks and hears you.
4. Grant Microphone, Accessibility and Input Monitoring to the terminal; tap Control-Option-Space.
5. `./install-mac.sh --pill` and `--german` separately; each only on request.
6. With Homebrew absent (rename it), the script stops with its install command and changes nothing.
7. Fill in the real download sizes (the Whisper model's is not recorded in this repo).

## Real Mac run, 2026-11-05 (FELIX-MAC, throwaway HOME, real Homebrew, Claude CLI stubbed)

- `--yes`: exit 0 in 4 min 13 s. Downloaded Whisper `ggml-large-v3-turbo-q5_0.bin` 560 MB; Kokoro model and voices 348 MB on disk; `.venv` 391 MB; uv cache 428 MB.
- Second `--yes`: 3 s, nothing downloaded or installed. It warned falsely about the Copilot CLI entry, fixed since (an existing entry is now recognised).
- `--pill`: pill venv 51 MB. `--german`: `kokoro-martin.onnx` 325,512,630 bytes and `voices-martin.npz` 522,506 bytes.
- Homebrew hidden: exit 1, prints the install command, nothing changed.
- Not run: first permission prompts, Control-Option-Space and a spoken turn from a fresh install. The health check answered from the live services on that Mac, so a fresh install's own services are untested.
- Caution: `voicemode-switch` shares fixed `/tmp/*.pid` files and ports with any other install on the machine, and `start` frees port 8880 by force. Running the installer beside a live setup needs care; FELIX-MAC used a sandbox that denied signals to other processes.
