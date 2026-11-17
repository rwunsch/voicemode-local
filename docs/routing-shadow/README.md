# Routing block shadowed by an earlier line in voicemode.env

## Summary

`voicemode-switch <mode>` writes the routing (`VOICEMODE_STT_BASE_URLS`,
`VOICEMODE_TTS_BASE_URLS`, sometimes `VOICEMODE_VOICES` / `VOICEMODE_TTS_TIMEOUT_LOCAL`) into a
managed block in `~/.voicemode/voicemode.env`. voice-mode's loader (`voice_mode/config.py`,
`load_voicemode_env`) keeps the **first** assignment of each key (`if key not in os.environ`), so
an active line for the same key above the block wins and the block is ignored.

Found 2026-11-17 (v1.2.22): the file had `VOICEMODE_STT_BASE_URLS=http://127.0.0.1:2022/v1` at
line 71 and the block said `2022,2023`; `from voice_mode import config; config.STT_BASE_URLS`
returned only 2022, so the STT CPU fallback was never routed. `VOICEMODE_TTS_BASE_URLS` (line 68)
was shadowed too, with the same value, so it had no effect there.

## What the switch does now

- **Supersedes in place.** For each key the new block writes, an active line outside the block
  that the loader would read becomes
  `# superseded by voicemode-switch managed block: <original line>` and the switch says which
  (`Commented out (...): VOICEMODE_STT_BASE_URLS (line 71)`). Comments, other keys, multiline
  values and `export ...` lines (which the loader does not read) are left alone. A re-run leaves
  the file byte-identical.
- **Restores.** When a later block no longer writes a key, a line with exactly that marker
  prefix is put back verbatim. Anything else that merely looks like it stays as it is.
- **A value the user set still wins.** `VOICEMODE_VOICES` and `VOICEMODE_TTS_TIMEOUT_LOCAL` set
  outside the block are not written into it, so they are never superseded.
- **Checks the result.** Every mode switch and `voicemode-switch status` end with
  `Effective routing (as voice-mode reads ...)`: the STT/TTS lists the real `voice_mode.config`
  loads from the file (repo `.venv` python, or `VOICEMODE_CONFIG_PY`; this shell's `VOICEMODE_*`
  variables are left out). If a list differs from the block, one warning per key names the line
  that shadows it. If voice-mode cannot be imported it says `not checked` and does not fail.

## Gotchas

- Precedence is first-wins across files too: the global `~/.voicemode/voicemode.env` loads
  before any project `.voicemode.env`, so a project file cannot override a key the global file
  sets (upstream behaviour, not changed here).
- Real environment variables beat the file. The check deliberately ignores them; a
  `VOICEMODE_*_BASE_URLS` in `~/.claude.json`'s env block would still win in the MCP server
  (the switch already removes those).

## Tests

`tests/test_switch_routing_shadow.py` (fake HOME; uses the installed `voice_mode.config` and is
skipped where it is not importable).
