# Shared Voice Supervisor (Plan A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One cross-platform voice-process supervisor in voicemode-local (services, barge
trigger, push-to-talk listener; Windows host outside every chat), driven by a host profile,
so FELIX's `voice.py` and our `voicemode_native.py` both become thin profiles over it.

**Architecture:**
- A stdlib-only package `vml_supervisor/` at the repo root. psutil is optional and used when
  present, as today.
- FELIX's lifecycle is the origin and moves in first, with its newer fixes. Our Windows parts
  (Job Objects, watchdog restarts, signal-handler restore) are added.
- A JSON-serialisable `HostProfile` carries every host-specific value. The Windows host starts
  without an inherited environment, so it loads the profile from a file.
- The connectors (`voice.py launch`, `voicemode_native.py launch`) keep the lease protocol and
  call the package.

**Tech Stack:** Python 3.10+ (stdlib, optional psutil, ctypes on Windows), pytest. Windows
PowerShell 5.1 for `Win32_Process.Create`.

**Spec:** `docs/felix-integration/2026-11-12-one-supervisor-proposal.md` (approved by Robert,
2026-11-12: split yes, route i, Talos keeps reviewing vendor). The research map behind this plan
is FELIX `voice.py` at b36f288ce / 8c9dce0e2 (identical lifecycle) against our
`scripts/windows/voicemode_native.py` at main.

## Global Constraints

- **The base is FELIX's host fix.** FELIX's lifecycle source is #1743 at the commit carrying
  FELIX-voice's Windows host fix (`voice.py host`, Win32_Process.Create). Until that commit is
  named, nothing past Task 1 starts. Pin it in Task 1.
- **Vendored byte-identical.** The package is vendored by FELIX unchanged at a tag. No file in
  it may contain the text `OPENAI_API_KEY` or `CARTESIA_API_KEY` (FELIX condition 2).
- **No silent fallbacks** (condition 3). Every fallback is returned as a recorded result:
  breakaway refused, CIM refused, a port taken, a helper failing, a restart.
- **The package writes no FE-facing text** (condition 5). It returns
  `Result(code: str, detail: str)`. The profile owner words it.
- **Processes:**
  - Children start with `CREATE_NO_WINDOW` on Windows.
  - Connector stdio passes through (`connector_options`).
  - Services bind 127.0.0.1 only.
  - Readiness checks use a proxy-free opener.
- **Ports come from the profile.** FELIX: 18880-18899 / 12022-12041 plus `<root>/ports.json`.
  voicemode-local: 28880-28899 / 22022-22041 plus `VOICEMODE_NATIVE_{TTS,STT}_PORT`. Neither
  may use 8880, 8881, 8882, 2022 or 9000.
- **The working folder of every child is `profile.working_dir`**, never the connector's current
  folder: on Windows that folder is searched first, and it may be a customer's repo.
- **Fix-everything rule** (Robert, 2026-11-11): bugs found on the way are fixed test-first, not
  deferred.

## Review Focus

1. **Breakaway or `Win32_Process.Create` refused** (strict job, CIM blocked by policy). Expected:
   a recorded `Result`, services still started where possible, and the profile owner told.
   Never a silent run inside the chat's job. Tested in Task 4 and Task 7.
2. **Two connectors start at the same moment.** Expected: one host, one TTS, one STT, both
   leases recorded. Tested in Task 5 (lock) and Task 7 (host start race).
3. **The host dies while leases remain.** Expected: the next connector `ensure` starts a new
   host; recorded ports are reused when free; no orphan services. Tested in Task 7.
4. **`state.json` or a lease written by PowerShell** (byte-order mark), or a corrupt file.
   Expected: a BOM is read; a corrupt file is a recorded `Result` and treated as empty state
   only under the lock, never a crash in `release`. Tested in Task 2.
5. **A chat started in a customer repo that holds a `python.exe` or `whisper-server.exe`.**
   Expected: children start in `profile.working_dir`, and the repo's binaries are never run.
   Tested in Task 5.

---

### Task 1: Pin the base, branch, package skeleton, `HostProfile`

**Files:**
- Create: `vml_supervisor/__init__.py`, `vml_supervisor/profile.py`, `vml_supervisor/result.py`
- Create: `tests/test_supervisor_profile.py`
- Modify: `docs/superpowers/plans/2026-11-12-shared-voice-supervisor.md` (record the base commit)

**Interfaces:**
- Produces: `Result(code: str, detail: str = "", data: dict = {})` (a frozen dataclass), and
  `Results` (a list collecting them).
- Produces: `HostProfile`, a frozen dataclass with `to_file(path: Path) -> None`,
  `from_file(path: Path) -> HostProfile` and `validate() -> list[Result]`. Fields:
  - paths: `name: str`, `root: Path`, `run_dir: Path`, `runtime_dir: Path`, `python: Path`,
    `working_dir: Path`;
  - `env: dict[str, str]`;
  - `services: dict[str, ServiceSpec]` (keys `tts`, `stt`);
  - `helpers: dict[str, HelperSpec]` (keys `barge`, `ptt`);
  - `reserved_ports: tuple[int, ...]`;
  - `port_override: PortOverride` (`kind: "file" | "env" | "none"`, `path`, `keys`);
  - `start_timeout: float`, `stop_timeout: float`, `watch_interval: float`;
  - `restart_limit: tuple[int, float]` (count, window seconds);
  - `lease_markers: tuple[str, ...]`, `voice_server_marker: str`;
  - `host_command: list[str]`, `host_marker: str`, `watch_command: list[str]`,
    `watch_marker: str`;
  - `job_name: str`;
  - `host_start_grace: float` (60 s);
  - `mode_source: ModeSource` (`kind: "json" | "env_file" | "none"`, `path`, `key`).
- Produces: `ServiceSpec(argv: list[str], marker: str, port_range: tuple[int, int],
  ready_path: str, ready_kind: "kokoro" | "whisper")`. `argv` may contain the placeholder
  `{port}`.
- Produces: `HelperSpec(argv: list[str], marker: str, log: str, platforms: tuple[str, ...],
  needs_mode: bool)`.

- [ ] **Step 1:** Get the host-fix commit from FELIX-voice, then check that it adds
  `voice.py host`: `git -C /home/wunsch/git/felix-framework show <commit> --stat`. Write the
  commit hash here, under Spec, as "Base". Run `git -C /home/wunsch/git/voicemode-local checkout
  -b feature/shared-supervisor main`.
- [ ] **Step 2: Write failing tests** in `tests/test_supervisor_profile.py`:
  - `test_a_profile_round_trips_through_its_file`: `from_file(to_file(p)) == p`.
  - `test_validate_rejects_reserved_and_overlapping_ranges`: a TTS range containing 8880 gives
    a Result with `code == "port_range_reserved"`.
  - `test_the_package_names_no_provider_key`: no file under `vml_supervisor/` contains
    `"OPENAI" + "_API_KEY"` or `"CARTESIA" + "_API_KEY"`.
  - `test_the_package_imports_only_the_stdlib`: an AST walk of every import finds only stdlib
    modules plus a guarded `psutil`.
- [ ] **Step 3:** Run `.venv/bin/python -m pytest tests/test_supervisor_profile.py -q`.
  Expected: FAIL (`ModuleNotFoundError: vml_supervisor`).
- [ ] **Step 4:** Implement the dataclasses. `to_file` writes JSON atomically; `Path` values
  are written as strings.
- [ ] **Step 5:** Run again. Expected: all pass.
- [ ] **Step 6:** Commit: `feat(supervisor): package skeleton, HostProfile, Result`.

### Task 2: State, lock and leases

**Files:**
- Create: `vml_supervisor/state.py`
- Test: `tests/test_supervisor_state.py`

**Interfaces:**
- Consumes: `HostProfile`, `Result`.
- Produces:
  - `service_lock(run_dir) -> ContextManager`: `msvcrt.locking` / `fcntl.flock` on
    `run/services.lock`.
  - `atomic_json(path, value)`: retries 20 × 50 ms on `PermissionError`, as ours does.
  - `read_json(path, default) -> (value, Result | None)`: `utf-8-sig`. A corrupt file returns
    `(default, Result("state_corrupt", path))`.
  - `write_lease(profile, pid, data)`.
  - `live_leases(profile) -> list[int]`. A lease lives while its pid's command contains any
    of `profile.lease_markers`, or its `child`'s command contains
    `profile.voice_server_marker`. Others are removed; names starting with `.` are skipped.

- [ ] **Step 1: Failing tests:**
  - `test_a_bom_written_lease_is_read`: a lease written as `﻿{"child": 5}` reads `child == 5`.
  - `test_a_corrupt_state_is_a_result_not_a_crash`: returns the default plus
    `code == "state_corrupt"`.
  - `test_the_lock_holds_a_second_taker`: a second process cannot take it non-blocking (port of
    FELIX's `test_voicemode_windows.py` L69, run here on POSIX).
  - `test_a_killed_launchers_voice_server_keeps_its_lease`: FELIX test 1062 pins this.
  - `test_a_dead_empty_lease_is_removed`.
- [ ] **Step 2:** Run `pytest tests/test_supervisor_state.py -q`. Expected: FAIL (import).
- [ ] **Step 3:** Implement. Move FELIX's `exclusive` / `service_lock` / `live_leases` and add
  the `utf-8-sig` and retry behaviour. `live_leases` takes `process_command` from Task 3 through
  an argument that defaults to the real one, so the tests can inject.
- [ ] **Step 4:** Run. Expected: pass.
- [ ] **Step 5:** Commit: `feat(supervisor): state, lock, leases (BOM-safe, corrupt state is a result)`.

### Task 3: Process primitives and ports

**Files:**
- Create: `vml_supervisor/procs.py`, `vml_supervisor/ports.py`
- Test: `tests/test_supervisor_procs.py`

**Interfaces:**
- Produces in `procs`:
  - `process_command(pid) -> str | None`: psutil on Windows; POSIX `kill(pid, 0)` then
    `/bin/ps -ww -o command=`.
  - `process_tree(pid) -> set[int]` and `kill_tree(pid, timeout)`.
  - `owned(entry: dict, command_of=process_command) -> bool`: type-checked as ours is.
  - `listener_pids(port) -> set[int]`.
  - `port_free(port) -> bool`.
  - `stop_signals_held() -> ContextManager` and `unblock_stop_signals()`: FELIX's POSIX
    `pthread_sigmask`; no-ops on Windows.
  - `connector_options() -> dict` and `background_options() -> dict`.
- Produces in `ports`:
  - `port_candidates(profile, kind) -> (list[int], Result | None)`: the override first
    (validated as FELIX does: an int, not a bool, 1024-65535, not reserved), then the range.
  - `choose_port(profile, kind, taken: set[int]) -> int | None`.

- [ ] **Step 1: Failing tests:**
  - `test_override_validation`: a table of a bool, a string, 80, 70000, 8880 and a valid
    value, against the expected Results. From FELIX test 744.
  - `test_a_taken_port_is_left_alone`: from FELIX test 757; a busy loopback socket is skipped.
  - `test_one_port_is_never_chosen_for_two_services`: from FELIX test 1042.
  - `test_owned_needs_the_marker_and_the_port`: from FELIX test 775. Also, non-int pids give
    False.
  - `test_a_child_has_stop_signals_unblocked`: POSIX; from FELIX test 1346.
- [ ] **Step 2:** Run. Expected: FAIL (import).
- [ ] **Step 3:** Implement by moving the functions. Where the copies differ, take the version
  marked better in the research map: FELIX for signals, ours for the `owned` type checks.
- [ ] **Step 4:** Run. Expected: pass.
- [ ] **Step 5:** Commit: `feat(supervisor): process and port primitives`.

### Task 4: Windows Job Objects and a spawn that records breakaway

**Files:**
- Create: `vml_supervisor/winjob.py`
- Test: `tests/test_supervisor_winjob.py` (fakes); `tests/test_supervisor_winjob_windows.py`
  (real Windows; skipped elsewhere; run with `tests/run_windows_transport_tests.sh`)

**Interfaces:**
- Produces: `Job(name: str | None, breakaway: bool = False, silent_breakaway: bool = False)`
  with `assign(handle)` and `close()`, moved from ours.
- Produces: `spawn(argv, job: Job | None, **popen_kwargs) -> (Popen, Result)`.
  - `Result.code` is `"breakaway"` (it left the parent's job), `"breakaway_refused"` (the
    parent's job forbids it; the child stays in that job too), or `"no_job"`.
  - A failed `assign` kills the child and returns `Result("job_assign_failed")`; it is never
    started outside our job.
- Produces: `services_job(profile) -> Job | None`, with name `profile.job_name`.

- [ ] **Step 1: Failing tests (fakes, every platform):**
  - `test_breakaway_refused_is_recorded`: `Popen` raises `PermissionError` on the first call
    with `CREATE_BREAKAWAY_FROM_JOB`; assert `code == "breakaway_refused"` and that the process
    is still assigned.
  - `test_a_failed_assign_kills_and_reports`.
- [ ] **Step 2: Failing test (Windows):** `test_a_child_spawned_with_breakaway_is_not_in_the_parents_job`.
  It uses `IsProcessInJob`, with a control run without breakaway.
- [ ] **Step 3:** Run the fakes. Expected: FAIL (import).
- [ ] **Step 4:** Implement by moving `Job`, `_resume_threads` and `spawn` from ours. Replace
  the silent retry with the recorded Result.
- [ ] **Step 5:** Run the fakes (pass). Run the Windows file through
  `tests/run_windows_transport_tests.sh`. Expected: pass. Write down if that cannot run here.
- [ ] **Step 6:** Commit: `feat(supervisor): Job Objects; spawn records a refused breakaway (no silent fallback)`.

### Task 5: Services: ensure, release, stop

**Files:**
- Create: `vml_supervisor/services.py`
- Test: `tests/test_supervisor_services.py`, with one fake world: real loopback HTTP fakes as in
  FELIX's `services` fixture (L151), a stand-in connector, and `FAKE_SERVICE` answering both
  readiness shapes.

**Interfaces:**
- Consumes: Tasks 2-4.
- Produces: `ensure_services(profile, connector_pid: int) -> (dict[str, int], list[Result])`.
  - Under the lock, in this order: write the lease, check that `runtime_dir/.complete.json`
    still exists (ours), start the watchdog (POSIX; the Windows host replaces it, Task 7), then
    reuse each owned service or start it on `choose_port`.
  - Each start uses `spawn` with `cwd=profile.working_dir` inside `stop_signals_held`, then is
    recorded in `state.json` as `{pid, port, marker, log, command, runtime}`. If recording
    fails: `kill_tree`.
  - Outside the lock: wait for readiness through the proxy-free opener, then the port-owner
    check. When no listener is found at all, the code is `owner_unknown`, not
    `owner_other`.
- Produces: `release(profile, connector_pid) -> list[Result]` and `stop(profile) -> list[Result]`
  (`stop` ends everything recorded, whatever leases remain).
- Produces: `check_service(kind, port) -> bool` and `valid_response(kind, data) -> bool`, moved
  unchanged.

- [ ] **Step 1: Failing tests:** ports of FELIX's generic tests 681, 699, 716, 787, 904, 925,
  945, 975 and 1030. Keep their names, prefixed `test_` (e.g.
  `test_services_stay_while_another_connector_runs`). Add:
  - `test_children_start_in_the_profiles_working_dir_not_the_callers` (Review Focus 5): put a
    fake `whisper-server` in the caller's folder and assert it never runs.
  - `test_two_connectors_at_once_start_one_tts_and_one_stt` (Review Focus 2): two threads calling
    `ensure_services`.
- [ ] **Step 2:** Run. Expected: FAIL (import).
- [ ] **Step 3:** Implement by moving FELIX's `ensure_services` and adding ours: the
  `.complete.json` guard, the isinstance guard on `taken`, and the job. The commands come from
  `ServiceSpec.argv`, with `{port}` filled in.
- [ ] **Step 4:** Run. Expected: pass.
- [ ] **Step 5:** Commit: `feat(supervisor): services ensure/release/stop moved from FELIX with ours merged`.

### Task 6: Helpers (barge trigger, push-to-talk listener)

**Files:**
- Create: `vml_supervisor/helpers.py`
- Test: `tests/test_supervisor_helpers.py` (FELIX's `_helper_world` fakes, L1279)

**Interfaces:**
- Produces: `start_helper(profile, kind, mode: str | None) -> Result`.
  - Under the lock with signals held: an owned entry in the same mode is a no-op. Otherwise
    stop the old one, `stop_unrecorded`, spawn into the services job, and record.
  - Outside the lock: a 3 s liveness check.
  - `OSError` gives `Result("helper_failed")`; it never raises.
- Produces: `stop_helper(profile, kind) -> Result`, and
  `stop_unrecorded(profile, kind) -> list[Result]`. The folder boundary is `root + os.sep`,
  case-folded on Windows.
- Produces: `read_mode(profile) -> str | None`, from `profile.mode_source`. A JSON key path
  such as `voice.barge`, or an env-file key.

- [ ] **Step 1: Failing tests:** ports of FELIX's 1219, 1306, 1318, 1330, 1338, 1346 and 1394
  under the same names. Add `test_read_mode_from_json_and_env_file`.
- [ ] **Step 2:** Run. Expected: FAIL (import).
- [ ] **Step 3:** Implement by moving FELIX's `start_helper`, `stop_unrecorded`,
  `running_commands` and `stop_entry`. The helper commands come from `HelperSpec`.
- [ ] **Step 4:** Run. Expected: pass.
- [ ] **Step 5:** Commit: `feat(supervisor): helpers with mode, liveness check, unrecorded cleanup`.

### Task 7: Watchdog (POSIX) and Windows host

**Files:**
- Create: `vml_supervisor/host.py`
- Test: `tests/test_supervisor_host.py` (fakes); `tests/test_supervisor_host_windows.py` (real
  Windows)

**Interfaces:**
- Consumes: Tasks 2-6.
- Produces: `watch_main(profile)`, the POSIX watchdog. Every `watch_interval`, under the lock:
  no live lease means `stop` and exit; otherwise restart a dead service on its recorded port
  within `restart_limit`. Each restart is a Result in `run/watch.log`.
- Produces: `host_main(profile_file: Path)`, the Windows host. FELIX's host fix (base commit)
  is moved in, plus:
  - it owns `services_job`, the services and the helpers as its children;
  - the watchdog restarts;
  - `read_mode` every `watch_interval`, restarting a helper when its mode changed;
  - it writes `run/helpers.json` (`{kind: {state, code, detail}}`);
  - it stops everything when no lease is left after `host_start_grace`.
- Produces: `start_host(profile) -> Result`. Through `Win32_Process.Create`, run via Windows
  PowerShell by its full path with `New-CimInstance Win32_ProcessStartup -ClientOnly
  @{ShowWindow=0}`, as FELIX's `voice-install.js` L120-137 does.
  - It rejects arguments containing `"` or a newline.
  - Its arguments are `profile.host_command + [str(profile_file)]`.
  - A refusal is `Result("host_start_refused")`, with no fallback.
- Produces: `ensure(profile, connector_pid) -> (dict[str, int], list[Result])`, the connector
  side.
  - On Windows: write the lease, `start_host` if `run/host.pid` is not a live process carrying
    `host_marker`, then wait for ready ports, up to `start_timeout`.
  - Elsewhere: `ensure_services` plus `start_helper` for each helper whose platform matches.

- [ ] **Step 1: Failing tests (fakes):**
  - `test_the_watchdog_stops_services_when_every_connector_was_killed`: FELIX 959.
  - `test_the_watchdog_restarts_a_dead_service_within_its_limit`: ours L589-616.
  - `test_host_stops_after_the_grace_when_no_lease_is_left`.
  - `test_host_restarts_a_helper_when_its_mode_changes`.
  - `test_a_refused_host_start_is_a_result`, with a fake PowerShell exit.
  - `test_a_dead_host_is_started_again_by_the_next_connector` (Review Focus 3).
  - `test_two_connectors_start_one_host` (Review Focus 2): `run/host.pid` is written under the
    lock.
- [ ] **Step 2: Failing test (Windows):**
  `test_host_started_via_cim_is_outside_the_callers_job_and_survives_it`. Start it from a
  process inside a `KILL_ON_JOB_CLOSE` job, close that job, and assert the host and services
  live on.
- [ ] **Step 3:** Run the fakes. Expected: FAIL (import).
- [ ] **Step 4:** Implement: move FELIX's host and our watch tick.
- [ ] **Step 5:** Run the fakes (pass) and the Windows test (pass, or write down why it can't
  run).
- [ ] **Step 6:** Commit: `feat(supervisor): POSIX watchdog and the Windows host outside every chat`.

### Task 8: voicemode_native.py becomes a thin profile

**Files:**
- Modify: `scripts/windows/voicemode_native.py`. Delete the moved functions; keep setup (Plan B
  moves it), settings, switches and `main`.
- Modify: `patches/manifest.windows.json` only if the package must be copied into the runtime
  (decide with Step 1).
- Test: `tests/test_voicemode_native.py`, `tests/test_voicemode_native_windows.py`

**Interfaces:**
- Consumes: `HostProfile`, `ensure`, `release`, `stop`, `start_helper`, `stop_helper`,
  `host_main`, `watch_main`.
- Produces: `personal_profile(root: Path, runtime: Path) -> HostProfile`. It holds the values
  in the research map's "Ours" column:
  - ranges 28880 / 22022;
  - `env` override keys `VOICEMODE_NATIVE_{TTS,STT}_PORT`;
  - markers `voicemode_native.py launch|serve|watch|host`;
  - job name `Local\voicemode-local-services-<sha16(root.lower())>`;
  - `mode_source` = env file `~/.voicemode/voicemode.env`, key `VOICEMODE_VOICE_BARGE`;
  - `working_dir = root / "voice-mode"`, created on first use.

- [ ] **Step 1:** Decide how the native launcher imports the package. The repo checkout path
  is on `sys.path` (the file already knows `REPO`). Record the ruling.
- [ ] **Step 2:** Rewrite the existing `FakeWorld` tests (`tests/test_voicemode_native.py`
  L208) to fake the package's seams instead of the old functions. Keep every assertion. Run
  them against the old code first: they must still pass.
- [ ] **Step 3:** Switch `launch`, `serve`, `watch`, `stop`, `status` and the switches to the
  package, then delete the moved code.
- [ ] **Step 4:** Run the full suite:
  `VM812_SRC=<flat> VOICEMODE_RELEASE_CHECK=1 .venv/bin/python -m pytest tests -q`. Expected: green.
- [ ] **Step 5:** Run the Windows tests through `tests/run_windows_transport_tests.sh`.
  Expected: green, or write down why it can't run.
- [ ] **Step 6:** Commit: `refactor(native): voicemode_native.py is a profile over vml_supervisor`.

### Task 9: FELIX profile fixture, parity check, docs, release

**Files:**
- Create: `tests/test_supervisor_felix_profile.py`
- Modify: `docs/felix-integration/README.md` (new section "What v1.2.0 changes"),
  `docs/windows-native/README.md`, the proposal (status)
- Create: `vml_supervisor/README.md` (the profile fields, entry points and Result codes, as a
  table)

**Interfaces:**
- Consumes: everything.
- Produces: `felix_profile_example(root, runtime) -> HostProfile`, a test fixture with FELIX's
  values from the research map's FELIX column. FELIX owns the real one in its `voice.py`.

- [ ] **Step 1: Failing test:**
  `test_felix_values_validate_and_run_the_service_lifecycle`. Use the fake world from Task 5
  with FELIX's ranges, its `ports.json` override and the JSON `mode_source` (`voice.barge`).
- [ ] **Step 2:** Run. Expected: FAIL until the fixture exists. Then implement the fixture.
  Expected: pass.
- [ ] **Step 3:** Write `vml_supervisor/README.md`. List every `Result.code` with its
  meaning. FELIX words them.
- [ ] **Step 4:** Run the full suite (green). Then a fresh-reviewer final review of the whole
  branch.
- [ ] **Step 5:** Commit. On Robert's go: merge and tag `v1.2.0`. Send FELIX-voice the tag and
  the package's file list for vendoring. FELIX's step 3 (`voice.py` becomes a thin profile)
  follows in FELIX.
