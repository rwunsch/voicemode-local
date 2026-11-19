"""voicemode-switch diagnose / patches/stall_diagnose.py: a read-only snapshot taken during a
voice stall, before /mcp reconnect (2026-11-18).

Every external command, HTTP request, /proc read and clock is a fake: nothing here touches a
running voice service, process or the real ~/.voicemode. The end-to-end test runs the switch
with a fake HOME, stubbed commands first on PATH and engine ports remapped to a spare port served
by a tiny HTTP server inside this test process.
"""
import http.server
import importlib.util
import json
import os
import socket
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SWITCH = REPO / "voicemode-switch"
spec = importlib.util.spec_from_file_location("stall_diagnose", REPO / "patches" / "stall_diagnose.py")
sd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sd)

NOW = 1_795_100_000.0   # a fixed clock for every test


# -- fakes ------------------------------------------------------------------------------------

class FakeRun:
    """Answers commands by their first words; records every call with its timeout."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def __call__(self, cmd, timeout):
        self.calls.append((list(cmd), timeout))
        for n in (4, 3, 2, 1):
            key = " ".join(cmd[:n])
            if key in self.answers:
                out = self.answers[key]
                if isinstance(out, BaseException):
                    raise out
                return out if isinstance(out, tuple) else (0, out, "")
        return (127, "", f"{cmd[0]}: not found")


class FakeHttp:
    """(method, url) -> (status, body bytes) or an exception; seconds taken per url."""

    def __init__(self, answers=None, seconds=None):
        self.answers = answers or {}
        self.seconds = seconds or {}
        self.calls = []

    def __call__(self, method, url, body=None, headers=None, timeout=None):
        self.calls.append((method, url, body, timeout))
        out = self.answers.get((method, url), ConnectionRefusedError("refused"))
        if isinstance(out, BaseException):
            raise out
        return out


def make_env(tmp_path, *, system="Linux", run=None, http=None, which=None, load=(1.0, 1.0, 1.0),
             cores=14, uid=1000, ports=None, clock=None):
    home = tmp_path / "home"
    (home / ".voicemode" / "logs" / "conversations").mkdir(parents=True, exist_ok=True)
    proc = tmp_path / "proc"
    proc.mkdir(exist_ok=True)
    t = {"now": NOW}
    clock = clock or (lambda: t["now"])
    return sd.Env(home=home, system=system, run=run or FakeRun(), http=http or FakeHttp(),
                  which=which or (lambda name: None), loadavg=lambda: load, cpu_count=lambda: cores,
                  uid=uid, proc_root=proc, now=clock, monotonic=time.monotonic,
                  sleep=lambda s: None, ports=ports, clk_tck=100)


# -- Suspects: only measured values against thresholds written in the code --------------------

def facts(**kw):
    base = {"load": {"load1": 1.0, "cores": 14}, "engines": [], "streams_on_suspended": [],
            "procs": [], "log_events": [], "exchanges": []}
    base.update(kw)
    return base


def test_load_above_twice_the_cores_is_a_suspect():
    assert sd.LOAD_PER_CORE_SUSPECT == 2.0
    hit = sd.suspects(facts(load={"load1": 28.1, "cores": 14}), NOW)
    assert any("load 28.1 > 2.0 x 14 cores" in s for s in hit)
    assert sd.suspects(facts(load={"load1": 28.0, "cores": 14}), NOW) == []


def test_a_slow_or_failed_probe_of_a_configured_engine_is_a_suspect():
    slow = {"name": "Kokoro", "port": 8880, "kind": "tts", "configured": True, "health_ok": True,
            "probe_s": sd.TTS_PROBE_SLOW_S + 0.1, "probe_error": None, "load_at": 30.0}
    hit = sd.suspects(facts(engines=[slow]), NOW)
    assert any("8880" in s and "tts probe" in s for s in hit)
    fast = dict(slow, probe_s=sd.TTS_PROBE_SLOW_S - 0.1)
    assert sd.suspects(facts(engines=[fast]), NOW) == []
    failed = dict(slow, probe_s=None, probe_error="timed out")
    assert any("8880" in s and "timed out" in s for s in sd.suspects(facts(engines=[failed]), NOW))
    stt = {"name": "Whisper STT", "port": 2022, "kind": "stt", "configured": True, "health_ok": True,
           "probe_s": sd.STT_PROBE_SLOW_S + 0.1, "probe_error": None, "load_at": 3.0}
    assert any("2022" in s and "stt probe" in s for s in sd.suspects(facts(engines=[stt]), NOW))


def test_an_engine_that_is_not_configured_is_never_a_suspect():
    down = {"name": "STT CPU fallback", "port": 2023, "kind": "stt", "configured": False,
            "health_ok": False, "probe_s": None, "probe_error": "refused", "load_at": 1.0}
    assert sd.suspects(facts(engines=[down]), NOW) == []
    configured_down = dict(down, configured=True)
    assert any("2023" in s and "health" in s for s in sd.suspects(facts(engines=[configured_down]), NOW))


def test_a_stream_on_a_suspended_device_is_a_suspect():
    hit = sd.suspects(facts(streams_on_suspended=["source-output 7 on source 2 RDPSource (SUSPENDED)"]), NOW)
    assert any("SUSPENDED" in s for s in hit)


def test_thread_state_d_is_a_suspect():
    procs = [{"pid": 51336, "state": "S", "threads": {"R": 0, "S": 29, "D": 1}}]
    assert any("51336" in s and "D" in s for s in sd.suspects(facts(procs=procs), NOW))
    ok = [{"pid": 51336, "state": "S", "threads": {"R": 1, "S": 29, "D": 0}}]
    assert sd.suspects(facts(procs=ok), NOW) == []


def test_a_paerror_in_the_last_hour_is_a_suspect_an_older_one_is_not():
    recent = {"when": NOW - 600, "line": "Recording with VAD failed: ... [PaErrorCode -9987]"}
    old = {"when": NOW - sd.LOG_WINDOW_S - 1, "line": recent["line"]}
    assert any("PaErrorCode" in s for s in sd.suspects(facts(log_events=[recent]), NOW))
    assert sd.suspects(facts(log_events=[old]), NOW) == []


def test_the_fixed_duration_fallback_in_the_last_hour_is_a_suspect():
    ev = {"when": NOW - 60, "line": "INFO - Falling back to fixed duration recording"}
    hit = sd.suspects(facts(log_events=[ev]), NOW)
    assert any("fixed-duration" in s for s in hit)


def test_slow_exchanges_in_the_last_hour_are_suspects():
    tts = {"when": NOW - 100, "kind": "tts", "ttfa": 49.7, "gen": 49.3, "stt": None, "timing": "ttfa 49.7s"}
    assert any("ttfa 49.7" in s for s in sd.suspects(facts(exchanges=[tts]), NOW))
    stt = {"when": NOW - 100, "kind": "stt", "ttfa": None, "gen": None, "stt": sd.EXCHANGE_STT_SUSPECT_S + 1,
           "timing": "record 3.0s, stt 16.0s"}
    assert any("stt" in s for s in sd.suspects(facts(exchanges=[stt]), NOW))
    old = dict(tts, when=NOW - sd.LOG_WINDOW_S - 1)
    assert sd.suspects(facts(exchanges=[old]), NOW) == []


# -- parsers ----------------------------------------------------------------------------------

PACTL_SINKS = "1\tRDPSink\tmodule-rdp-sink.c\ts16le 2ch 44100Hz\tSUSPENDED\n"
PACTL_SOURCES = ("1\tRDPSink.monitor\tmodule-rdp-sink.c\ts16le 2ch 44100Hz\tSUSPENDED\n"
                 "2\tRDPSource\tmodule-rdp-source.c\ts16le 1ch 44100Hz\tSUSPENDED\n")
PACTL_SINK_INPUTS = ""
PACTL_SOURCE_OUTPUTS = "7\t2\t12\tprotocol-native.c\ts16le 1ch 24000Hz\n"


def test_pactl_streams_on_suspended_devices_are_named():
    found = sd.streams_on_suspended(PACTL_SINKS, PACTL_SOURCES, PACTL_SINK_INPUTS, PACTL_SOURCE_OUTPUTS)
    assert found == ["source-output 7 on source 2 RDPSource (SUSPENDED)"]
    running = PACTL_SOURCES.replace("RDPSource\tmodule-rdp-source.c\ts16le 1ch 44100Hz\tSUSPENDED",
                                    "RDPSource\tmodule-rdp-source.c\ts16le 1ch 44100Hz\tRUNNING")
    assert sd.streams_on_suspended(PACTL_SINKS, running, "", PACTL_SOURCE_OUTPUTS) == []


def test_timing_strings_are_parsed():
    assert sd.parse_timing("ttfa 3.8s, gen 6.2s, play 18.8s") == {"ttfa": 3.8, "gen": 6.2, "play": 18.8}
    assert sd.parse_timing("record 26.5s, stt 6.4s") == {"record": 26.5, "stt": 6.4}
    assert sd.parse_timing(None) == {}


def test_voice_mode_mcp_processes_are_told_from_others():
    assert sd.is_voice_mode_mcp("/x/.venv/bin/python3 /x/.venv/bin/voice-mode")
    assert sd.is_voice_mode_mcp("/x/bin/voice-mode")
    assert sd.is_voice_mode_mcp("python3 -m voice_mode")
    assert not sd.is_voice_mode_mcp("/x/python3 /x/voice_mode/voice_pill.py --stdio")
    assert not sd.is_voice_mode_mcp("vim /x/bin/voice-mode")
    assert not sd.is_voice_mode_mcp("grep voice-mode")


# -- collection ---------------------------------------------------------------------------------

def _proc(root, pid, comm, utime, stime, threads=1, state="S", task_states=("S",),
          vol=10, nonvol=5):
    d = root / str(pid)
    (d / "task").mkdir(parents=True, exist_ok=True)
    fields = [state, "1"] + ["0"] * 9 + [str(utime), str(stime)] + ["0"] * 4 + [str(threads)]
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n")
    (d / "status").write_text(f"Name:\t{comm}\nState:\t{state} (sleeping)\nThreads:\t{threads}\n"
                              f"voluntary_ctxt_switches:\t{vol}\nnonvoluntary_ctxt_switches:\t{nonvol}\n")
    for i, s in enumerate(task_states):
        t = d / "task" / str(pid + i)
        t.mkdir(exist_ok=True)
        (t / "stat").write_text(f"{pid + i} ({comm}) {s} 1 0\n")


PS_LINUX = (
    "  101     1  1000  50000    1  0.1 /sbin/init\n"
    "  200   101  1000   9000   12  2.0 claude --resume\n"
    "  300   200  1000    909   30  3.0 /x/.venv/bin/python3 /x/.venv/bin/voice-mode\n"
    "  400   101  1001   1000   30  0.0 /y/python3 /y/bin/voice-mode\n"
    "  500   101  1000    100    1  0.0 /x/python3 /x/voice_mode/voice_pill.py --stdio\n"
)


def test_voice_mode_processes_with_owner_proc_state_and_threads(tmp_path):
    run = FakeRun({"ps -eo": PS_LINUX})
    env = make_env(tmp_path, run=run)
    _proc(env.proc_root, 300, "voice-mode", 100, 50, threads=3, task_states=("S", "D", "R"),
          vol=5695, nonvol=3078)
    sessions = env.home / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "200.json").write_text(json.dumps({"pid": 200, "sessionId": "abcdef123456",
                                                   "cwd": "/home/u/git/proj", "name": "proj-work"}))
    data = sd.collect_processes(env, cpu={300: 4.5})
    by_pid = {p["pid"]: p for p in data["procs"]}
    assert set(by_pid) == {300, 400}
    p = by_pid[300]
    assert p["threads"] == {"R": 1, "S": 1, "D": 1}
    assert p["state"] == "S"
    text = "\n".join(data["lines"])
    assert "pid 300" in text and "ppid 200" in text and "4.5%" in text
    assert "voluntary 5695" in text and "nonvoluntary 3078" in text
    assert "proj-work" in text and "abcdef12" in text and "/home/u/git/proj" in text
    assert "R 1, S 1, D 1" in text


def test_py_spy_missing_is_said_and_never_installed(tmp_path):
    run = FakeRun({"ps -eo": PS_LINUX})
    env = make_env(tmp_path, run=run, which=lambda n: None)
    lines = sd.collect_pyspy(env, [300, 400])
    assert lines == ["  py-spy not installed: pip install py-spy"]
    assert not any(c[0][0] in ("pip", "pip3", "py-spy") for c in run.calls)


def test_py_spy_dumps_only_own_processes_nonblocking_with_10s_timeout(tmp_path):
    run = FakeRun({"/bin/py-spy dump": "Thread 1 (idle): MainThread\n    select (selectors.py:468)\n"})
    env = make_env(tmp_path, run=run, which=lambda n: "/bin/py-spy" if n == "py-spy" else None, uid=1000)
    procs = [{"pid": 300, "uid": 1000}, {"pid": 400, "uid": 1001}]
    lines = sd.collect_pyspy(env, procs)
    dumps = [c for c in run.calls if c[0][:2] == ["/bin/py-spy", "dump"]]
    assert [c[0] for c in dumps] == [["/bin/py-spy", "dump", "--nonblocking", "--pid", "300"]]
    assert dumps[0][1] == sd.PYSPY_TIMEOUT_S == 10
    text = "\n".join(lines)
    assert "MainThread" in text and "400: not dumped (another user's process)" in text


def test_audio_on_mac_and_windows_is_not_collected(tmp_path):
    for system in ("Darwin", "Windows"):
        env = make_env(tmp_path, system=system, which=lambda n: None)
        data = sd.collect_audio(env)
        assert any("not collected on this OS" in line for line in data["lines"])
        assert data["streams_on_suspended"] == []


def test_audio_on_linux_lists_devices_states_and_streams(tmp_path):
    run = FakeRun({"pactl info": "Server Name: pulseaudio\nDefault Sink: RDPSink\nDefault Source: RDPSource\n",
                   "pactl list short sinks": PACTL_SINKS, "pactl list short sources": PACTL_SOURCES,
                   "pactl list short sink-inputs": PACTL_SINK_INPUTS,
                   "pactl list short source-outputs": PACTL_SOURCE_OUTPUTS})
    env = make_env(tmp_path, run=run, which=lambda n: "/usr/bin/pactl" if n == "pactl" else None)
    data = sd.collect_audio(env)
    text = "\n".join(data["lines"])
    assert "Default Source: RDPSource" in text
    assert "sink 1 RDPSink: SUSPENDED" in text and "source 2 RDPSource: SUSPENDED" in text
    assert "sink-inputs: 0" in text and "source-outputs: 1" in text
    assert data["streams_on_suspended"] == ["source-output 7 on source 2 RDPSource (SUSPENDED)"]
    assert all(t <= sd.CMD_TIMEOUT_S for _, t in run.calls)


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone().isoformat()


def test_exchanges_show_timings_flag_the_slowest_three_and_never_the_spoken_text(tmp_path):
    env = make_env(tmp_path)
    from datetime import datetime
    day = datetime.fromtimestamp(NOW).strftime("%Y-%m-%d")
    path = env.home / ".voicemode/logs/conversations" / f"exchanges_{day}.jsonl"
    rows = []
    for i in range(14):
        kind = "tts" if i % 2 else "stt"
        timing = f"ttfa {i}.0s, gen {i}.5s, play 1.0s" if kind == "tts" else f"record 30.0s, stt 0.{i}s"
        rows.append({"timestamp": _iso(NOW - 1000 + i), "conversation_id": f"conv_{i}", "type": kind,
                     "text": f"SECRET WORDS {i}", "metadata": {"timing": timing,
                                                             "provider_url": "http://127.0.0.1:8880/v1"}})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    data = sd.collect_exchanges(env)
    text = "\n".join(data["lines"])
    assert "SECRET" not in text
    assert len([ln for ln in data["lines"] if "conv_" in ln]) == 12
    assert "conv_0 " not in text and "conv_1 " not in text      # the last 12 only
    flagged = sorted((ln for ln in data["lines"] if "slowest #" in ln), key=lambda ln: ln.split("slowest #")[1])
    assert len(flagged) == 3
    assert "ttfa 13.0s" in flagged[0] and "ttfa 11.0s" in flagged[1] and "ttfa 9.0s" in flagged[2]
    assert len(data["exchanges"]) == 12


def test_debug_log_keeps_the_last_20_matching_lines_once_each(tmp_path):
    env = make_env(tmp_path)
    lines = []
    from datetime import datetime
    for i in range(30):
        stamp = datetime.fromtimestamp(NOW - 3000 + i).strftime("%Y-%m-%d %H:%M:%S,000")
        msg = f"{stamp} - voicemode - ERROR - Recording with VAD failed: Wait timed out [PaErrorCode -9987] {i}"
        lines += [msg, msg]                      # the debug log writes many lines twice
        lines.append(f"{stamp} - voicemode - INFO - fine {i}")
    (env.home / "voicemode_debug.log").write_text("\n".join(lines) + "\n")
    data = sd.collect_debug_log(env)
    shown = [ln for ln in data["lines"][1:] if "PaErrorCode" in ln]
    assert len(shown) == 20 and shown[-1].endswith(" 29")
    assert len(data["log_events"]) == 20
    assert abs(data["log_events"][-1]["when"] - (NOW - 3000 + 29)) < 1


def test_debug_log_never_copies_spoken_text_and_install_is_not_a_stall(tmp_path):
    # Found on the first live run (2026-11-18): "stall" matched "installed", and a "TTS full text:"
    # line that happened to contain a pattern word copied the spoken text into the report.
    env = make_env(tmp_path)
    from datetime import datetime
    stamp = datetime.fromtimestamp(NOW - 10).strftime("%Y-%m-%d %H:%M:%S,000")
    (env.home / "voicemode_debug.log").write_text("\n".join([
        f"{stamp} - voicemode - WARNING - voice fallback: voice bf_x is not installed here",
        f"{stamp} - voicemode - DEBUG - TTS full text: my request timed out, SECRET",
        f"{stamp} - voicemode - INFO -   Transcribed: it stalled again SECRET",
        f"{stamp} - voicemode - ERROR - Response text: timed out SECRET",
        f"{stamp} - voicemode - WARNING - TTS stalled for 12 s",
    ]) + "\n")
    data = sd.collect_debug_log(env)
    body = data["lines"][1:]
    assert not any("SECRET" in ln for ln in body)
    assert not any("installed" in ln for ln in body)
    assert len(body) == 1 and "TTS stalled" in body[0]


def test_engine_probes_health_identity_latency_and_load(tmp_path):
    http = FakeHttp({
        ("GET", "http://127.0.0.1:8880/health"): (200, b'{"status":"ok","engine":"kokoro-onnx","provider":"CUDAExecutionProvider"}'),
        ("GET", "http://127.0.0.1:8880/v1/audio/voices"): (200, b'{"voices":["af_heart","af_sky"]}'),
        ("POST", "http://127.0.0.1:8880/v1/audio/speech"): (200, b"RIFF...."),
        ("GET", "http://127.0.0.1:2022/health"): (200, b'{"status":"ok","engine":"faster-whisper","model":"small","device":"cuda"}'),
        ("POST", "http://127.0.0.1:2022/v1/audio/transcriptions"): (200, b'{"text":""}'),
    })
    (tmp_path / "home/.voicemode").mkdir(parents=True)
    (tmp_path / "home/.voicemode/voicemode.env").write_text(
        "VOICEMODE_TTS_BASE_URLS=http://127.0.0.1:8880/v1,http://127.0.0.1:8881/v1\n"
        "VOICEMODE_STT_BASE_URLS=http://127.0.0.1:2022/v1\n")
    env = make_env(tmp_path, http=http, load=(19.5, 17.0, 14.0),
                   ports=[("Whisper STT", 2022, "stt"), ("Kokoro", 8880, "tts"), ("Piper", 8881, "tts")])
    data = sd.collect_engines(env)
    by_port = {e["port"]: e for e in data["engines"]}
    assert by_port[8880]["health_ok"] and by_port[8880]["probe_error"] is None
    assert by_port[8880]["load_at"] == 19.5
    assert by_port[8881]["configured"] and not by_port[8881]["health_ok"]
    speech = [c for c in http.calls if c[1].endswith("/audio/speech")]
    assert json.loads(speech[0][2])["voice"] == "af_sky" and speech[0][3] == sd.PROBE_TIMEOUT_S == 15
    stt = [c for c in http.calls if c[1].endswith("/audio/transcriptions")][0]
    assert b"RIFF" in stt[2] and b'name="file"' in stt[2]
    text = "\n".join(data["lines"])
    assert "CUDAExecutionProvider" in text and "faster-whisper" in text and "load 19.5" in text
    assert "8881" in text and "not answering" in text


def test_silent_wav_is_one_second_of_16k_mono():
    import io
    import wave
    with wave.open(io.BytesIO(sd.silent_wav()), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 16000, 16000)


def test_the_whole_report_never_raises_when_everything_fails(tmp_path):
    def broken(*a, **k):
        raise OSError("boom")
    env = make_env(tmp_path, run=broken, http=broken, which=lambda n: "/bin/" + n,
                   ports=[("Kokoro", 8880, "tts")])
    env.loadavg = broken
    text, found, path = sd.diagnose(env)
    for section in ("== Load", "== Top CPU", "== voice-mode MCP processes", "== Engines",
                    "== Audio path", "== Exchanges", "== Debug log", "== Conch", "== py-spy", "== Suspects"):
        assert section in text, section
    assert "failed" in text
    assert path.parent == env.home / ".voicemode" / "logs" and path.name.startswith("diagnose-")
    assert path.read_text() == text


def test_conch_status_comes_from_the_voicemode_cli_with_a_timeout(tmp_path):
    run = FakeRun({"/v/voicemode conch status": "Conch: free\nWaiters: none\n"})
    env = make_env(tmp_path, run=run, which=lambda n: None)
    env.voicemode_cli = "/v/voicemode"
    lines = sd.collect_conch(env)
    assert "  Conch: free" in lines
    assert run.calls[-1] == (["/v/voicemode", "conch", "status"], sd.CONCH_TIMEOUT_S)


def test_top_cpu_on_linux_is_sampled_over_one_interval(tmp_path):
    env = make_env(tmp_path)
    _proc(env.proc_root, 10, "busy", 100, 0, threads=4)
    _proc(env.proc_root, 11, "idle", 500, 0, threads=2)

    def advance(_s):
        _proc(env.proc_root, 10, "busy", 180, 0, threads=4)     # 80 ticks in 1 s at 100 Hz = 80 %
        _proc(env.proc_root, 11, "idle", 501, 0, threads=2)
    env.sleep = advance
    data = sd.collect_top(env)
    assert data["cpu"][10] == pytest.approx(80.0)
    assert "busy" in data["lines"][1] and "80.0%" in data["lines"][1] and "threads 4" in data["lines"][1]


# -- Windows host (WSL only, 2026-11-19) ---------------------------------------------------------
# A real stall coincided with WSL's PulseAudio not answering for 5 s while the Windows host CPU
# was at 100 %, so the host side belongs in the report. powershell.exe is always a fake here.

def host_json(**kw):
    base = {"load": 97, "logical": 16, "physical": 8, "interval": 1.0, "commit_pct": 71.5,
            "top": [{"name": "chrome", "pid": 4100, "cpu_s": 6.4},
                    {"name": "vmmemWSL", "pid": 3000, "cpu_s": 8.0},
                    {"name": "Teams", "pid": 5200, "cpu_s": 1.6},
                    {"name": "Taskmgr", "pid": 6000, "cpu_s": 0.8}],
            "vmmem": [{"name": "vmmemWSL", "pid": 3000, "ws": 12884901888}],
            "services": [{"name": "Audiosrv", "status": "Running"},
                         {"name": "AudioEndpointBuilder", "status": "Running"}]}
    base.update(kw)
    return json.dumps(base)


def wsl_env(tmp_path, answer, *, took=2.0, environ=None):
    """A WSL env whose powershell.exe answers `answer` and takes `took` seconds."""
    t = {"mono": 100.0}
    run = FakeRun({"powershell.exe -NoProfile -Command": answer})

    def timed(cmd, timeout):
        if cmd[0] == "powershell.exe":
            t["mono"] += took
        return run(cmd, timeout)
    env = make_env(tmp_path, run=timed)
    env.monotonic = lambda: t["mono"]
    env.environ = {"WSL_DISTRO_NAME": "Ubuntu-22.04"} if environ is None else environ
    return env, run


def test_wsl_is_detected_from_the_distro_variable_or_proc_version(tmp_path):
    env = make_env(tmp_path)
    env.environ = {}
    assert not sd.is_wsl(env)
    env.environ = {"WSL_DISTRO_NAME": "Ubuntu"}
    assert sd.is_wsl(env)
    env.environ = {}
    (env.proc_root / "version").write_text("Linux version 6.6.87.2-microsoft-standard-WSL2 (gcc)\n")
    assert sd.is_wsl(env)
    env.system = "Darwin"
    assert not sd.is_wsl(env)


def test_the_host_section_is_not_collected_outside_wsl(tmp_path):
    env, run = wsl_env(tmp_path, host_json(), environ={})
    data = sd.collect_windows_host(env)
    assert data["lines"] == ["  not collected on this OS (not WSL)"]
    assert data["host"] is None
    assert not any(c[0][0] == "powershell.exe" for c in run.calls)
    text, _, _ = sd.diagnose(env)
    assert "== Windows host\n  not collected on this OS (not WSL)" in text


def test_the_host_section_reports_load_processes_memory_services_and_call_time(tmp_path):
    env, run = wsl_env(tmp_path, host_json(), took=3.4)
    data = sd.collect_windows_host(env)
    ps = [c for c in run.calls if c[0][0] == "powershell.exe"]
    assert len(ps) == 1
    assert ps[0][0][:3] == ["powershell.exe", "-NoProfile", "-Command"]
    assert ps[0][1] == sd.POWERSHELL_TIMEOUT_S == 20
    text = "\n".join(data["lines"])
    assert "powershell.exe took 3.4 s" in text
    assert "host CPU load 97%" in text and "16 logical / 8 physical" in text
    assert "committed memory 71.5%" in text
    assert "vmmemWSL pid 3000 working set 12.0 GB" in text
    assert "Audiosrv Running" in text and "AudioEndpointBuilder Running" in text
    # cpu_s over the interval, as a share of the whole host: 6.4 s / 1 s / 16 = 40 %
    chrome = next(ln for ln in data["lines"] if "chrome" in ln)
    assert "4100" in chrome and "40.0%" in chrome
    top = data["host"]["top"]
    assert [p["name"] for p in top] == ["vmmemWSL", "chrome", "Teams", "Taskmgr"]
    assert data["host"]["load"] == 97 and data["host"]["ps_s"] == pytest.approx(3.4)


def test_the_host_top_list_has_at_most_eight_processes(tmp_path):
    many = [{"name": f"p{i}", "pid": 100 + i, "cpu_s": float(i)} for i in range(12)]
    env, _ = wsl_env(tmp_path, host_json(top=many))
    data = sd.collect_windows_host(env)
    assert len(data["host"]["top"]) == sd.TOP_N == 8
    assert data["host"]["top"][0]["name"] == "p11"


def test_the_host_script_and_report_carry_no_command_lines_or_window_titles(tmp_path):
    for word in ("CommandLine", "MainWindowTitle", "Win32_Process ", "Win32_Process'", ".Path",
                 "StartInfo", "Get-WmiObject", "Arguments"):
        assert word not in sd.HOST_SCRIPT, word
    leaky = json.loads(host_json())
    leaky["top"][0].update(CommandLine="chrome.exe --token=SECRET", MainWindowTitle="SECRET mail",
                           Path="C:\\SECRET\\chrome.exe")
    env, _ = wsl_env(tmp_path, json.dumps(leaky))
    data = sd.collect_windows_host(env)
    assert "SECRET" not in "\n".join(data["lines"])
    assert "SECRET" not in json.dumps(data["host"])


def test_the_host_script_uses_no_double_quotes_or_newlines():
    # It is one argument passed from WSL to a Windows command line: keep quoting trivial.
    assert '"' not in sd.HOST_SCRIPT and "\n" not in sd.HOST_SCRIPT
    assert "ConvertTo-Json" in sd.HOST_SCRIPT and "Get-Service" in sd.HOST_SCRIPT
    assert "Win32_Processor" in sd.HOST_SCRIPT and "LoadPercentage" in sd.HOST_SCRIPT


def test_a_powershell_timeout_is_a_line_and_a_suspect_never_an_error(tmp_path):
    env, _ = wsl_env(tmp_path, subprocess.TimeoutExpired("powershell.exe", 20), took=20.0)
    data = sd.collect_windows_host(env)
    text = "\n".join(data["lines"])
    assert "timed out after 20 s" in text
    assert data["host"]["ps_s"] == pytest.approx(20.0)
    hit = sd.suspects(facts(host=data["host"]), NOW)
    assert any("powershell.exe took 20.0 s > 10.0 s" in s for s in hit)


def test_a_failing_or_missing_powershell_is_a_line(tmp_path):
    env, _ = wsl_env(tmp_path, (1, "", "Access is denied."))
    text = "\n".join(sd.collect_windows_host(env)["lines"])
    assert "failed (exit 1)" in text and "Access is denied" in text
    env, _ = wsl_env(tmp_path, FileNotFoundError(2, "No such file", "powershell.exe"))
    text = "\n".join(sd.collect_windows_host(env)["lines"])
    assert "failed: FileNotFoundError" in text


@pytest.mark.parametrize("out", ["", "garbage, not json", "[1, 2, 3]", '{"load": "high", "top": 7}',
                                 '{"top": [{"name": null, "pid": "x", "cpu_s": "y"}, 5],'
                                 ' "services": "Running", "vmmem": {"ws": "big"}}'])
def test_malformed_powershell_output_is_a_line_not_an_error(tmp_path, out):
    env, _ = wsl_env(tmp_path, out)
    data = sd.collect_windows_host(env)
    assert data["lines"]
    sd.suspects(facts(host=data["host"]), NOW)   # must not raise


def test_powershell_noise_before_the_json_is_skipped(tmp_path):
    env, _ = wsl_env(tmp_path, "\ufeffWARNING: something\r\n" + host_json() + "\r\n")
    assert "host CPU load 97%" in "\n".join(sd.collect_windows_host(env)["lines"])


def host_fact(**kw):
    base = {"wsl": True, "ps_s": 2.0, "ps_error": None, "load": 50, "logical": 16, "physical": 8,
            "top": [], "services": {"Audiosrv": "Running", "AudioEndpointBuilder": "Running"}}
    base.update(kw)
    return base


def test_host_cpu_at_ninety_percent_is_a_warn_suspect():
    assert sd.HOST_CPU_SUSPECT_PCT == 90.0
    hit = sd.suspects(facts(host=host_fact(load=90)), NOW)
    assert any("WARN" in s and "Windows host CPU load 90% >= 90.0%" in s for s in hit)
    assert sd.suspects(facts(host=host_fact(load=89)), NOW) == []


def test_a_non_wsl_process_over_a_quarter_of_the_host_is_a_suspect():
    assert sd.HOST_PROC_SUSPECT_PCT == 25.0
    top = [{"name": "chrome", "pid": 4100, "pct": 25.1}, {"name": "vmmemWSL", "pid": 3000, "pct": 60.0},
           {"name": "wsl", "pid": 3100, "pct": 30.0}, {"name": "Teams", "pid": 5200, "pct": 25.0}]
    hit = sd.suspects(facts(host=host_fact(top=top)), NOW)
    assert any("chrome" in s and "4100" in s and "25.1%" in s for s in hit)
    assert not any("vmmemWSL" in s or "Teams" in s or "pid 3100" in s for s in hit)


def test_an_audio_service_that_is_not_running_is_a_suspect():
    hit = sd.suspects(facts(host=host_fact(services={"Audiosrv": "Stopped",
                                                    "AudioEndpointBuilder": "Running"})), NOW)
    assert any("Audiosrv" in s and "Stopped" in s for s in hit)
    missing = sd.suspects(facts(host=host_fact(services={"AudioEndpointBuilder": "Running"})), NOW)
    assert any("Audiosrv" in s and "not found" in s for s in missing)


def test_a_slow_powershell_call_is_a_suspect():
    assert sd.POWERSHELL_SLOW_S == 10.0
    assert any("powershell.exe took 10.5 s > 10.0 s" in s
               for s in sd.suspects(facts(host=host_fact(ps_s=10.5)), NOW))
    assert sd.suspects(facts(host=host_fact(ps_s=10.0)), NOW) == []


def test_no_host_facts_means_no_host_suspects():
    assert sd.suspects(facts(host=None), NOW) == []
    assert sd.suspects(facts(), NOW) == []


def test_the_whole_report_has_the_host_section_when_powershell_fails(tmp_path):
    def broken(*a, **k):
        raise OSError("boom")
    env, _ = wsl_env(tmp_path, host_json())
    env.run = broken
    text, _, _ = sd.diagnose(env)
    assert "== Windows host" in text and "failed" in text.split("== Windows host")[1].split("==")[0]


# -- the switch, end to end ----------------------------------------------------------------------

def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class _Kokoro(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"ok","engine":"kokoro-onnx","provider":"CUDAExecutionProvider"}' \
            if self.path == "/health" else b'{"voices":["af_sky"]}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Length", "4")
        self.end_headers()
        self.wfile.write(b"RIFF")

    def log_message(self, *a):
        pass


def _spare_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_switch_diagnose_writes_the_report_and_prints_its_path(tmp_path):
    home, fakebin = tmp_path / "home", tmp_path / "bin"
    fakebin.mkdir()
    (home / ".voicemode").mkdir(parents=True)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Kokoro)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    dead = _spare_port()
    (home / ".voicemode/voicemode.env").write_text(f"VOICEMODE_TTS_BASE_URLS=http://127.0.0.1:{port}/v1\n")
    _stub(fakebin / "ps", f'echo "  4242     1  $(id -u)  100  3  0.0 {tmp_path}/python3 {tmp_path}/bin/voice-mode"')
    for name in ("pactl", "ss", "py-spy"):
        _stub(fakebin / name, "exit 1")
    _stub(fakebin / "voicemode", 'echo "Conch: free"')
    # Under WSL the switch asks the Windows host: never the real powershell.exe in a test.
    _stub(fakebin / "powershell.exe", 'echo \'{"load": 12, "logical": 4, "physical": 2, "interval": 1.0}\'')
    env = {**os.environ, "HOME": str(home), "PATH": f"{fakebin}:{os.environ['PATH']}",
           "VOICEMODE_DIAGNOSE_PORTS": f"Kokoro:{port}:tts,Dead:{dead}:stt"}
    try:
        r = subprocess.run([str(SWITCH), "diagnose"], env=env, capture_output=True, text=True, timeout=120)
    finally:
        server.shutdown()
    assert r.returncode == 0, r.stderr
    reports = list((home / ".voicemode/logs").glob("diagnose-*.txt"))
    assert len(reports) == 1
    assert f"Report written to {reports[0]}" in r.stdout
    text = reports[0].read_text()
    # The switch prefers the checkout's own .venv/bin/voicemode when it exists (a real checkout has one, a
    # fresh worktree does not), so the conch section is either the stub's line or the real CLI's answer.
    assert "== Conch" in text and ("Conch: free" in text or "the conch is free" in text)
    assert "kokoro-onnx" in text and "pid 4242" in text
    assert f"Dead ({dead}, stt, not configured): not answering" in text and "== Suspects" in text
    assert "== Windows host" in text
    if os.environ.get("WSL_DISTRO_NAME"):
        assert "host CPU load 12%" in text
