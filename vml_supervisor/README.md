# vml_supervisor

The shared core lifecycle for the voice processes of any harness: the TTS and STT services, the
optional helpers (barge-in trigger, push-to-talk listener), the connector leases, the watchdog on
POSIX and the Windows host that keeps everything outside every chat. Stdlib only; `psutil` is
needed on Windows. It knows nothing about a particular harness: voicemode-local's Claude Code
profile is `vml_profiles/claude.py`, FELIX owns its own.

## Extension pattern

Three layers: the **core** (this package), a **profile** (a `HostProfile`: ports, commands,
markers, timings, plain data) and **named hooks** (`"module:function"` entry points in
`profile.hooks`, for what data cannot say: the runtime to use, the environment, a mode, a
pre-start check, a result sink). Every hook is optional and has a default; a failing hook is a
recorded `Result`, never an exception. The hooks are loaded once by the host and kept, so an
update replacing the hook folder cannot change a running host. The exact contract (arguments,
return shapes, what is loaded from `hook_paths`) is the module docstring of `hooks.py`.

## HostProfile fields

| Field | Meaning |
|---|---|
| `name` | The harness's name. |
| `root`, `run_dir`, `runtime_dir`, `working_dir` | The install, the folder for state files (leases, state, pids, logs), the runtime the processes run from, and their working folder. |
| `python` | The interpreter that runs the helpers and the host. |
| `env` | The environment services and helpers get. |
| `services` | `{"tts": ServiceSpec, "stt": ServiceSpec}`: argv (with `{port}`), ownership marker, port range, readiness path and kind (`kokoro` or `whisper`). |
| `helpers` | `{"barge": HelperSpec, "ptt": HelperSpec}`: argv, marker, log, platforms, whether a mode decides it, optional status command. |
| `reserved_ports` | Ports no range may contain, added to the stack's own (8880, 8881, 8882, 2022, 9000). |
| `port_override` | `PortOverride`: `file` (a JSON `{kind: port}`), `env` (variable names for tts, stt) or `none`. |
| `start_timeout`, `stop_timeout` | Seconds to wait for readiness and for a stop. |
| `watch_interval`, `restart_limit` | Seconds between looks; `(count, window seconds)` of restarts allowed. |
| `lease_markers` | Command-line markers of a connector process; a lease is live only while one matches. |
| `voice_server_marker` | Marker of the voice server process. |
| `host_command`, `host_marker` | How the Windows host is started and recognised. |
| `watch_command`, `watch_marker` | How the POSIX watchdog is started and recognised. |
| `job_name` | Windows Job Object name for the processes. |
| `host_start_grace` | Seconds a just-started host is given before it counts as missing. |
| `schema_version` | The profile file format (a newer one is refused). |
| `hooks`, `hook_paths` | Hook names to entry points, and the folders searched for them. |

## Entry points

| Entry point | Module | Purpose |
|---|---|---|
| `ensure(profile, connector_pid)` | `host` | A connector's entry: lease, services and (off Windows) helpers. Returns `(ports, results)`. |
| `release(profile, connector_pid)` | `services` | Drop the connector's lease; the last one out stops the services. |
| `stop(profile)` | `services` | Stop the host, the services and the helpers, whatever leases remain. |
| `start_helper(profile, kind, mode, hooks)` | `helpers` | Start one helper in a mode; one `Result`. |
| `stop_helper(profile, kind, hooks)` | `helpers` | Stop one helper, recorded or not; one `Result`. |
| `host_main(profile_file)` | `host` | The Windows host's loop (`host <profile file>`). |
| `watch_main(profile)` | `host` | The POSIX watchdog's loop (`watch <profile file>`). |
| `load_profile(path)` | `profile` | Read a profile file without raising: `(profile, results)`. |
| `load_hooks(profile)` | `hooks` | Resolve the profile's hooks: `(Hooks, results)`. |

## Result codes

Every operation returns `Result(code, detail, data)` objects instead of raising.

| Code | Meaning |
|---|---|
| `breakaway` | A Windows process was started outside its parent's job. |
| `breakaway_refused` | The parent's job forbids breakaway; the child also dies with the parent. |
| `helper_command_bad` | A helper's command could not be built or started. |
| `helper_not_running` | A helper was asked to stop and was not running. |
| `helper_status_failed` | A helper's status command failed or printed nothing. |
| `helper_stopped` | A helper was stopped. |
| `helper_unrecorded_stopped` | A helper with no record was found by its marker and stopped. |
| `helpers_write_failed` | The helper records could not be written. |
| `hook_bad_return` | A hook returned the wrong shape; the default was used. |
| `hook_failed` | A hook raised or exited; the default was used. |
| `hook_unknown` | The profile names a hook this package does not have. |
| `host_command_bad` | The profile's host command is empty or unusable. |
| `host_command_unowned` | The host command does not carry the host marker, so the host could never be found again. |
| `host_failed` | The host stopped on an error. |
| `host_marker_empty` | The host marker is empty; the host pid file is left alone. |
| `host_not_configured` | The profile has no host command. |
| `host_pid_write_failed` | The host's pid could not be recorded. |
| `host_profile_write_failed` | The host's profile file could not be written. |
| `host_requires_psutil` | Windows without `psutil`: a running host cannot be found, so none was started. |
| `host_restart_limit` | The host was started too often in the window; not started again. |
| `host_start_refused` | The host could not be started. |
| `host_started` | The host was started. |
| `host_started_late` | The host was started but its start was not confirmed in time. |
| `host_stopped` | The host was stopped. |
| `job_assign_failed` | A process could not be put into its Job Object and was killed. |
| `job_unavailable` | The Job Object is unavailable; processes start without it. |
| `kill_incomplete` | A process was still alive after the kill. |
| `lease_remove_failed` | A connector's lease file could not be removed. |
| `lease_write_failed` | A connector's lease file could not be written. |
| `no_free_port` | No port in the service's candidates is free. |
| `no_job` | No Job Object was used (not Windows, or none given). |
| `owner_other` | Another process answers on the service's port. |
| `owner_unknown` | The port answered but no listener could be found to prove it is ours. |
| `port_override_invalid` | The port override is unreadable or not a usable port. |
| `port_range_reserved` | A service's range contains a reserved port. |
| `port_ranges_overlap` | Two services' ranges overlap. |
| `pre_start_refused` | The `pre_start` hook returned refusal text; the start was blocked. |
| `process_list_unavailable` | The process list could not be read. |
| `profile_key_unknown` | The profile file has a key this version does not know. |
| `profile_malformed` | The profile file is unreadable or not a profile. |
| `profile_schema_unsupported` | The profile file's schema is newer than this package supports. |
| `psutil_missing` | `psutil` is not installed (needed on Windows for process lookups and stops). |
| `restart_limit_reached` | A service died too often in the window; not restarted. |
| `restart_port_taken` | A service died and its port is now taken; not restarted there. |
| `resume_failed` | A suspended Windows process could not be resumed and was killed. |
| `runtime_incomplete` | The runtime folder has no `.complete.json`. |
| `runtime_not_current` | The `resolve_runtime` hook says the runtime is not the current one. |
| `service_command_bad` | A service's marker is empty; not started. |
| `service_command_unowned` | The service command lacks the marker or `--port N`; not started. |
| `service_exited` | A service exited while starting. |
| `service_moved` | A service now runs on another port than recorded. |
| `service_not_ready` | A service did not answer in `start_timeout`. |
| `service_restart_failed` | A dead service could not be restarted. |
| `service_restarted` | A dead service was restarted. |
| `service_start_failed` | A service could not be started. |
| `services_stopped` | `stop` finished; data says what was running. |
| `state_corrupt` | A state file is not valid JSON or not an object; read as empty. |
| `state_write_failed` | The state could not be written (a newly started service is killed). |
| `unknown_helper` | The profile has no such helper. |
| `unknown_service` | The profile has no such service. |
| `watch_failed` | The watchdog stopped on an error. |
| `watch_lock_failed` | The run folder's lock could not be taken. |
| `watch_marker_empty` | The watchdog marker is empty. |
| `watch_stopped` | The watchdog stopped. |
| `watchdog_not_configured` | The profile has no watch command. |
| `watchdog_start_failed` | The watchdog could not be started. |
| `working_dir_unavailable` | The working folder could not be created. |

`tests/test_supervisor_felix_profile.py` fails when a code in the package is missing from this
table.

## Stability

The profile schema and the hook interface are stable within a minor version. A breaking change
to either is made only in a new minor version and is listed in the release notes
(`docs/felix-integration/README.md`).

## Vendoring

A harness copies `vml_supervisor/*` byte-identical at a release tag and never patches it locally.
Differences belong in the profile and its hooks.
