# Task 4b report

## Implemented
- profile.py: removed ModeSource/mode_source; added schema_version (=1, SCHEMA_VERSION), hooks dict, hook_paths tuple[Path,...], HOOK_NAMES, validate() -> Result("hook_unknown", name), load_profile(path) -> (profile|None, [Result]) with profile_schema_unsupported / profile_malformed (no exceptions). _decode handles tuple[Path,...]. from_file stays strict.
- hooks.py (new): Hooks, load_hooks (imports once, hook_paths on sys.path then restored, per-hook failure is Result("hook_failed")), call_hook (never raises), typed wrappers with defaults for all seven hooks (resolve_runtime, environment, service_command, prepare_home, read_mode, on_result), run_pre_start.
- __init__.py: dropped ModeSource export.

## TDD
RED: 23 failed (unexpected kwarg hooks / missing hooks module / load_profile) before implementation.
GREEN: 26 passed in test_supervisor_profile.py + test_supervisor_hooks.py. Full suite: 1793 passed, 65 skipped, 3 xfailed.

## Files
vml_supervisor/profile.py, hooks.py (new), __init__.py; tests/test_supervisor_profile.py, tests/test_supervisor_hooks.py (new).

## Concerns
- run_pre_start returns Result("hook_failed") when the pre_start hook itself raises; the caller must decide whether that blocks the start (not blocked by this helper).
- Loaded modules stay in sys.modules (that is how "loaded once" survives file replacement); two profiles using the same module name in one process would share it.
- Wrapper signatures take (hooks, profile, ...) and return (value, Result|None); the brief gave only call_hook's signature.
- docs/superpowers/plans/...shared-voice-supervisor.md still mentions mode_source (plan text, not updated).

## Fix round 1
RED: 9 failed in hooks tests (profile tests were written after the profile edits, so they passed at once). GREEN: 41 passed (hooks + profile).
1. Shape checks in each wrapper (_checked): hook_bad_return + default; one test per wrapper (7 params).
2. from_dict (so from_file) raises ValueError on schema_version > 1.
3. load_profile reports profile_key_unknown per unknown top-level key and still loads (mode_source test).
4. call_hook catches SystemExit; KeyboardInterrupt passes.
5. schema_version < 1 and 6. string hook_paths: profile_malformed.
7. service_command default computed lazily (still KeyError for an unknown kind when the default is needed).
8. dead assert dropped. 9. Hook contract documented in hooks.py docstring, incl. the ruling that a raising pre_start does not block.
