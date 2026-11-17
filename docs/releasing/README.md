# Releasing voicemode-local

A release is not finished until it is applied to the install this machine actually runs.
Until v1.2.6 the steps below stopped at the GitHub Release, and the local `.venv` ran
2026-11-12 code through six releases (found 2026-11-13). Robert, 2026-11-13: "apply
locally to every release."

## Steps

Merges, tags and pushes to `main` need Robert's go in the session doing them; a go relayed
by another session does not count.

1. **Full suite on the branch**, with the pristine-source checks on:
   ```bash
   VM812_SRC=<pristine voice-mode 8.12.0 dir> VOICEMODE_RELEASE_CHECK=1 \
     .venv/bin/python -m pytest tests -q -p no:cacheprovider
   ```
   `VM812_SRC` is a flat folder of pristine 8.12.0 files, `tools/X.py` stored as `X.py`. Keep
   it outside /tmp (a cleared /tmp took it on 2026-11-16) and build it like this. The core
   package must win name collisions (`conch.py`, `providers.py`, `statistics.py` exist in both
   `voice_mode/` and `voice_mode/tools/`); copying `tools/` last made the tests patch the MCP
   tool's `conch.py` and fail with "an anchor was found 0 times":
   ```bash
   D=~/.cache/voicemode-local/vm812
   uv pip install --python .venv/bin/python3 --target "$D/pkg" --no-deps "voice-mode==8.12.0"
   mkdir -p "$D/flat"
   for f in "$D"/pkg/voice_mode/tools/*.py; do
     [ "$(basename "$f")" = __init__.py ] || cp "$f" "$D/flat/"
   done
   cp "$D"/pkg/voice_mode/*.py "$D/flat/"     # core last: it wins
   ```
   Also `node --test tests/pill_model.test.mjs` when `ui/pill/` changed, and
   `tests/run_windows_transport_tests.sh -k <one word>` when `pill-windows.ps1` changed
   (the runner splits its arguments on spaces).
2. **Merge** with `git merge --no-ff <branch>` into `main`, then run the full suite again on
   the merged result.
3. **Tag** with an annotated tag whose message is the release notes:
   `git tag -a vX.Y.Z -m "vX.Y.Z: …"`.
   **Name every changed default** in the notes, one line each, in this form:
   `behaviour change: <what>, old behaviour via <SETTING>=<value>`
   (for example `behaviour change: the installer turns on the CPU fallbacks with a native
   GPU engine, old behaviour via VOICEMODE_CPU_FALLBACK=false`). Go through the diff since
   the last tag for changed defaults, new defaults that are on, and anything that now stops
   or refuses where it used to go ahead. Where no setting restores the old behaviour, say so
   (`old behaviour: none`). The same lines go into the message to FELIX-voice (step 7).
4. **Push**: `gh auth switch --user rwunsch`, then `git push origin main vX.Y.Z`. Check
   with `git ls-remote origin refs/heads/main refs/tags/vX.Y.Z`.
5. **GitHub Release** from the tag notes:
   ```bash
   git tag -l --format='%(contents)' vX.Y.Z > notes.md
   gh release create vX.Y.Z -R rwunsch/voicemode-local --verify-tag \
     --notes-file notes.md --title vX.Y.Z --latest
   ```
   (`--notes-from-tag` cannot be combined with `-R` in gh 2.72.)
6. **Apply locally.** Reinstall pristine voice-mode, then patch it. A plain apply on an
   already patched venv keeps old patch text (see `docs/patching/README.md`):
   ```bash
   uv pip install --python .venv/bin/python3 --reinstall-package voice-mode "voice-mode==8.12.0"
   ./patches/apply.sh
   ```
   Then check that the release's own markers are in
   `.venv/lib/python3.12/site-packages/voice_mode/`, by grepping for the text each patch adds.
   Running voice-mode servers keep the code they loaded: a session gets the new code when
   its voice mode reconnects (`/mcp`) or the session restarts. Say so in the release message.
7. **Tell FELIX-voice** the tag and what changed, including every `behaviour change:` line
   from the tag notes, word for word. FELIX takes releases through its own pin.
