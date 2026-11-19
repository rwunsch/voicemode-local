"""A read-only snapshot of the voice path, to be taken DURING a stall, before /mcp reconnect.

    voicemode-switch diagnose          (or: python stall_diagnose.py [--voicemode-cli PATH])

One plain-text report, printed and written to ~/.voicemode/logs/diagnose-<timestamp>.txt.
Sections: load and cores; top CPU processes; under WSL the Windows host (CPU load, top processes
by name, committed memory, vmmem, audio services, via powershell.exe); every voice-mode MCP process (owner, /proc state,
thread states); engine health, identity and one small latency probe each; the audio path (pactl);
the last exchanges with their timings; matching lines of ~/voicemode_debug.log; the conch; a
py-spy dump of the user's own voice-mode processes when py-spy is installed; and Suspects.

Rules (2026-11-18):
- Read only. The probes are one short TTS sentence per TTS engine and one second of generated
  silence per STT engine; nothing is started, stopped, signalled or installed.
- Every step has a timeout and records its own failure; the report is always written.
- Suspects come only from measured values against the thresholds below. They are not causes.
- The spoken text in the exchanges log is never copied into the report: timings only.

Stdlib only (vendored by FELIX). Reusable from Python: diagnose(Env.default()) returns
(text, suspects, path); collect_* return {"lines": [...], <facts>}.
"""
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from datetime import datetime
from pathlib import Path

# -- thresholds (every Suspect line names the one it crossed) ----------------------------------
LOAD_PER_CORE_SUSPECT = 2.0     # 1-min load above this many times the core count
TTS_PROBE_SLOW_S = 5.0          # one short sentence; NOT derived from a measured warm time
STT_PROBE_SLOW_S = 5.0          # one second of silence; NOT derived from a measured warm time
EXCHANGE_TTFA_SUSPECT_S = 10.0  # a real turn's time to first audio (exchanges log)
EXCHANGE_STT_SUSPECT_S = 15.0   # a real turn's transcription time (exchanges log)
LOG_WINDOW_S = 3600.0           # log events and exchanges count only within the last hour
# Windows host (WSL only). 2026-11-18: a stall coincided with the host CPU at 100 % while WSL's
# own load looked survivable; these are first settings, not derived from measured stalls.
HOST_CPU_SUSPECT_PCT = 90.0     # Win32_Processor LoadPercentage (average), WARN
HOST_PROC_SUSPECT_PCT = 25.0    # one non-WSL Windows process, as a share of the whole host
POWERSHELL_SLOW_S = 10.0        # the powershell.exe call itself: slow under host overload

# -- timeouts ------------------------------------------------------------------------------------
CMD_TIMEOUT_S = 5.0
HEALTH_TIMEOUT_S = 3.0
PROBE_TIMEOUT_S = 15
CONCH_TIMEOUT_S = 20.0          # the voicemode CLI imports voice_mode: slow under load
PYSPY_TIMEOUT_S = 10
POWERSHELL_TIMEOUT_S = 20
TOP_SAMPLE_S = 1.0
TOP_N = 8
EXCHANGE_ROWS = 12
LOG_LINES = 20
DEBUG_TAIL_BYTES = 4 << 20
EXCHANGE_TAIL_BYTES = 1 << 20

DEFAULT_ENGINES = [("Whisper STT", 2022, "stt"), ("STT CPU fallback", 2023, "stt"),
                   ("Kokoro", 8880, "tts"), ("Piper", 8881, "tts"), ("Kokoro ONNX", 8882, "tts")]
PROBE_TEXT = "Hello."
PROBE_VOICE = "af_sky"
LOG_PATTERN = re.compile(r"PaErrorCode|Wait timed out|underrun|timed out|\bstall"
                         r"|Falling back to fixed duration")
# Debug-log lines that carry what was said or heard: never copied into the report.
SPOKEN_TEXT = re.compile(r"full text:|Transcribed:|Response text:|Turns result:|Survey result:")
LOG_STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "::1")
WSL_PROCESS_NAMES = ("vmmem", "vmmemwsl", "wsl", "wslhost", "wslservice", "wslrelay")
AUDIO_SERVICES = ("Audiosrv", "AudioEndpointBuilder")
# One line, single quotes only: it is passed from WSL as one argument to a Windows command line.
# Privacy: process names, ids and CPU seconds only. Never command lines, paths or window titles.
HOST_SCRIPT = (
    "$ErrorActionPreference='SilentlyContinue'; $r=@{}; "
    "$c=@(Get-CimInstance Win32_Processor); "
    "$r.load=($c|Measure-Object -Property LoadPercentage -Average).Average; "
    "$r.logical=($c|Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum; "
    "$r.physical=($c|Measure-Object -Property NumberOfCores -Sum).Sum; "
    "$a=@{}; foreach($p in Get-Process){$a[$p.Id]=$p.CPU}; "
    "$t=[Diagnostics.Stopwatch]::StartNew(); Start-Sleep -Milliseconds 1000; $b=@(Get-Process); "
    "$r.interval=$t.Elapsed.TotalSeconds; "
    "$r.top=@($b|Where-Object{$a.ContainsKey($_.Id) -and $null -ne $_.CPU -and $null -ne $a[$_.Id]}"
    "|ForEach-Object{[pscustomobject]@{name=$_.ProcessName;pid=$_.Id;cpu_s=[math]::Round($_.CPU-$a[$_.Id],3)}}"
    "|Sort-Object cpu_s -Descending|Select-Object -First 8); "
    "$r.commit_pct=(Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory).PercentCommittedBytesInUse; "
    "$r.vmmem=@(Get-Process -Name vmmem,vmmemWSL"
    "|ForEach-Object{[pscustomobject]@{name=$_.ProcessName;pid=$_.Id;ws=$_.WorkingSet64}}); "
    "$r.services=@(Get-Service -Name Audiosrv,AudioEndpointBuilder"
    "|ForEach-Object{[pscustomobject]@{name=$_.Name;status=[string]$_.Status}}); "
    "$r|ConvertTo-Json -Compress -Depth 4"
)


# -- the environment (everything external goes through here, so tests can fake it) --------------

def _run(cmd, timeout):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return p.returncode, p.stdout, p.stderr


def _http(method, url, body=None, headers=None, timeout=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # local: no proxy
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(1 << 20)
    except urllib.error.HTTPError as e:
        return e.code, e.read(65536)


def _engines_from_env():
    raw = os.environ.get("VOICEMODE_DIAGNOSE_PORTS", "").strip()
    if not raw:
        return list(DEFAULT_ENGINES)
    out = []
    for item in raw.split(","):
        parts = item.strip().split(":")
        if len(parts) == 3 and parts[1].isdigit() and parts[2] in ("tts", "stt"):
            out.append((parts[0], int(parts[1]), parts[2]))
    return out or list(DEFAULT_ENGINES)


class Env:
    def __init__(self, home, system, run, http, which, loadavg, cpu_count, uid, proc_root,
                 now, monotonic, sleep, ports=None, clk_tck=100, voicemode_cli=None, debug_log=None,
                 environ=None):
        self.home, self.system, self.run, self.http, self.which = Path(home), system, run, http, which
        self.environ = dict(environ) if environ is not None else {}
        self.loadavg, self.cpu_count, self.uid, self.proc_root = loadavg, cpu_count, uid, Path(proc_root)
        self.now, self.monotonic, self.sleep, self.clk_tck = now, monotonic, sleep, clk_tck
        self.ports = list(ports) if ports else list(DEFAULT_ENGINES)
        self.voicemode_cli = voicemode_cli
        self.debug_log = Path(debug_log) if debug_log else self.home / "voicemode_debug.log"

    @classmethod
    def default(cls, voicemode_cli=None):
        try:
            tck = os.sysconf("SC_CLK_TCK")
        except (AttributeError, ValueError, OSError):
            tck = 100
        return cls(home=Path.home(), system=platform.system(), run=_run, http=_http,
                   which=shutil.which, loadavg=os.getloadavg, cpu_count=os.cpu_count,
                   uid=os.getuid() if hasattr(os, "getuid") else None, proc_root="/proc",
                   now=time.time, monotonic=time.monotonic, sleep=time.sleep,
                   ports=_engines_from_env(), clk_tck=tck, voicemode_cli=voicemode_cli,
                   environ=os.environ)


def _cmd(env, cmd, timeout=CMD_TIMEOUT_S):
    """(stdout or None, note): None with a note when the command failed or timed out."""
    try:
        rc, out, err = env.run(cmd, timeout)
    except subprocess.TimeoutExpired:
        return None, f"{' '.join(cmd[:3])}: timed out after {timeout:g} s"
    except Exception as e:  # noqa: BLE001 - a missing tool is a line in the report
        return None, f"{' '.join(cmd[:3])}: failed: {type(e).__name__}: {e}"
    if rc != 0:
        tail = (err or out or "").strip().splitlines()
        return None, f"{' '.join(cmd[:3])}: failed (exit {rc}){': ' + tail[-1][:160] if tail else ''}"
    return out, ""


def _load1(env):
    try:
        return round(float(env.loadavg()[0]), 2)
    except Exception:  # noqa: BLE001
        return None


def _age(seconds):
    s = int(seconds)
    return f"{s // 86400}d {s % 86400 // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# -- load -------------------------------------------------------------------------------------------

def collect_load(env):
    cores = env.cpu_count()
    l1, l5, l15 = env.loadavg()
    lines = [f"  load {l1:.2f} {l5:.2f} {l15:.2f} (1/5/15 min), {cores} cores"]
    return {"lines": lines, "load": {"load1": round(float(l1), 2), "cores": cores}}


# -- top CPU ----------------------------------------------------------------------------------------

def _stat_fields(text):
    """(comm, fields after the comm) of a /proc/<pid>/stat line; fields[0] is the state."""
    head, _, rest = text.rpartition(")")
    return head.partition("(")[2], rest.split()


def _sample(root):
    out = {}
    for d in root.iterdir() if root.is_dir() else []:
        if not d.name.isdigit():
            continue
        try:
            comm, f = _stat_fields((d / "stat").read_text())
            out[int(d.name)] = (comm, int(f[11]) + int(f[12]), int(f[17]))
        except (OSError, ValueError, IndexError):
            continue
    return out


def collect_top(env):
    if env.system == "Linux" and env.proc_root.is_dir():
        start = env.monotonic()
        first = _sample(env.proc_root)
        env.sleep(TOP_SAMPLE_S)
        second = _sample(env.proc_root)
        interval = max(env.monotonic() - start, TOP_SAMPLE_S)
        cpu = {}
        for pid, (comm, ticks, threads) in second.items():
            if pid in first:
                cpu[pid] = (ticks - first[pid][1]) / env.clk_tck / interval * 100.0
        top = sorted(cpu, key=cpu.get, reverse=True)[:TOP_N]
        lines = [f"  sampled over {interval:.1f} s from /proc ({len(second)} processes)"]
        lines += [f"  {pid:>7} {second[pid][0][:20]:<20} {cpu[pid]:5.1f}%  threads {second[pid][2]}"
                  for pid in top]
        return {"lines": lines, "cpu": cpu}
    if env.system == "Darwin":
        out, note = _cmd(env, ["ps", "-Ao", "pid=,pcpu=,comm="])
        if out is None:
            return {"lines": [f"  {note}"], "cpu": {}}
        rows = []
        for ln in out.splitlines():
            p = ln.split(None, 2)
            if len(p) == 3 and p[0].isdigit():
                rows.append((float(p[1]), int(p[0]), p[2]))
        rows.sort(reverse=True)
        lines = ["  ps %cpu (decaying recent average); threads not collected on macOS"]
        lines += [f"  {pid:>7} {Path(comm).name[:20]:<20} {pc:5.1f}%" for pc, pid, comm in rows[:TOP_N]]
        return {"lines": lines, "cpu": {pid: pc for pc, pid, _ in rows}}
    return {"lines": [f"  not collected on this OS ({env.system})"], "cpu": {}}


# -- Windows host (WSL only) --------------------------------------------------------------------------

def is_wsl(env):
    if env.system != "Linux":
        return False
    if env.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in (env.proc_root / "version").read_text().lower()
    except OSError:
        return False


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _items(x):
    """ConvertTo-Json writes a one-element array as the element itself."""
    return [x] if isinstance(x, dict) else [i for i in x if isinstance(i, dict)] if isinstance(x, list) else []


def _host_json(out):
    for ln in reversed((out or "").replace("﻿", "").splitlines()):
        ln = ln.strip()
        if ln.startswith("{"):
            try:
                data = json.loads(ln)
            except ValueError:
                return None
            return data if isinstance(data, dict) else None
    return None


def collect_windows_host(env):
    if not is_wsl(env):
        return {"lines": ["  not collected on this OS (not WSL)"], "host": None}
    host = {"wsl": True, "ps_s": None, "ps_error": None, "load": None, "logical": None, "physical": None,
            "commit_pct": None, "top": [], "vmmem": [], "services": {}, "services_collected": False}
    t0 = env.monotonic()
    out, note = _cmd(env, ["powershell.exe", "-NoProfile", "-Command", HOST_SCRIPT], POWERSHELL_TIMEOUT_S)
    host["ps_s"] = round(env.monotonic() - t0, 2)
    lines = [f"  powershell.exe took {host['ps_s']:.1f} s (timeout {POWERSHELL_TIMEOUT_S:g} s)"]
    if out is None:
        host["ps_error"] = note
        return {"lines": lines + [f"  {note}"], "host": host}
    data = _host_json(out)
    if data is None:
        host["ps_error"] = "output not understood"
        lines.append(f"  powershell.exe output not understood ({len(out)} characters)")
        return {"lines": lines, "host": host}
    load, logical, physical = _num(data.get("load")), _num(data.get("logical")), _num(data.get("physical"))
    host.update(load=load, logical=int(logical) if logical else None, physical=int(physical) if physical else None,
                commit_pct=_num(data.get("commit_pct")))
    interval = _num(data.get("interval")) or TOP_SAMPLE_S
    top = []
    for p in _items(data.get("top")):
        name, pid, cpu_s = p.get("name"), p.get("pid"), _num(p.get("cpu_s"))
        if isinstance(name, str) and isinstance(pid, int) and cpu_s is not None:
            pct = cpu_s / interval / host["logical"] * 100.0 if host["logical"] else None
            top.append({"name": name, "pid": pid, "cpu_s": cpu_s, "pct": pct})
    host["top"] = sorted(top, key=lambda p: p["cpu_s"], reverse=True)[:TOP_N]
    for v in _items(data.get("vmmem")):
        if isinstance(v.get("name"), str) and isinstance(v.get("pid"), int):
            host["vmmem"].append({"name": v["name"], "pid": v["pid"], "ws": _num(v.get("ws"))})
    if isinstance(data.get("services"), (list, dict)):
        host["services_collected"] = True
        for s in _items(data.get("services")):
            if isinstance(s.get("name"), str) and isinstance(s.get("status"), str):
                host["services"][s["name"]] = s["status"]

    def fmt(x, unit=""):
        return f"{x:g}{unit}" if x is not None else "?"
    lines.append(f"  host CPU load {fmt(load, '%')} (Win32_Processor LoadPercentage, average), "
                 f"{host['logical'] or '?'} logical / {host['physical'] or '?'} physical processors")
    lines.append(f"  committed memory {fmt(host['commit_pct'], '%')}")
    for v in host["vmmem"]:
        ws = f"{v['ws'] / (1 << 30):.1f} GB" if v["ws"] is not None else "?"
        lines.append(f"  {v['name']} pid {v['pid']} working set {ws}")
    if not host["vmmem"]:
        lines.append("  vmmem / vmmemWSL: not seen")
    if host["services_collected"]:
        lines.append("  audio services: " + ", ".join(
            f"{name} {host['services'].get(name, 'not found')}" for name in AUDIO_SERVICES))
    else:
        lines.append("  audio services: not collected")
    lines.append(f"  top {len(host['top'])} processes by CPU over {interval:.1f} s "
                 "(% of the whole host; names only, no command lines or window titles)")
    for p in host["top"]:
        pct = f"{p['pct']:5.1f}%" if p["pct"] is not None else f"{p['cpu_s']:.2f} cpu s"
        lines.append(f"  {p['pid']:>7} {p['name'][:24]:<24} {pct}")
    return {"lines": lines, "host": host}


# -- voice-mode MCP processes -------------------------------------------------------------------------

def is_voice_mode_mcp(args):
    """The voice-mode MCP server: bin/voice-mode run directly or by a python, or python -m voice_mode."""
    a = args.split()
    if not a:
        return False
    first = os.path.basename(a[0])
    if first in ("voice-mode", "voice-mode.exe"):
        return True
    if "python" in first.lower() and len(a) > 1:
        return os.path.basename(a[1]) in ("voice-mode", "voice-mode.exe") or a[1:3] == ["-m", "voice_mode"]
    return False


def _parse_etime(s):
    """macOS ps etime: [[dd-]hh:]mm:ss -> seconds."""
    days, _, rest = s.rpartition("-")
    parts = [int(x) for x in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return (int(days) if days else 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def _ps_table(env):
    if env.system == "Linux":
        out, note = _cmd(env, ["ps", "-eo", "pid=,ppid=,uid=,etimes=,nlwp=,pcpu=,args="])
        ncols = 6
    elif env.system == "Darwin":
        out, note = _cmd(env, ["ps", "-axo", "pid=,ppid=,uid=,etime=,pcpu=,args="])
        ncols = 5
    else:
        return None, f"not collected on this OS ({env.system})"
    if out is None:
        return None, note
    table = {}
    for ln in out.splitlines():
        p = ln.split(None, ncols)
        if len(p) <= ncols or not p[0].isdigit():
            continue
        try:
            if ncols == 6:
                row = {"pid": int(p[0]), "ppid": int(p[1]), "uid": int(p[2]), "age": int(p[3]),
                       "nlwp": int(p[4]), "pcpu": float(p[5]), "args": p[6]}
            else:
                row = {"pid": int(p[0]), "ppid": int(p[1]), "uid": int(p[2]), "age": _parse_etime(p[3]),
                       "nlwp": None, "pcpu": float(p[4]), "args": p[5]}
        except ValueError:
            continue
        table[row["pid"]] = row
    return table, ""


def _is_claude(args):
    a = args.split()
    return bool(a) and (os.path.basename(a[0]) in ("claude", "claude.exe") or "claude-code" in args)


def _owner(env, table, row):
    cur = row
    for _ in range(4):
        parent = table.get(cur["ppid"])
        if parent is None:
            break
        if _is_claude(parent["args"]):
            try:
                s = json.loads((env.home / ".claude" / "sessions" / f"{parent['pid']}.json").read_text())
            except (OSError, ValueError):
                return f"claude pid {parent['pid']} (no session file)"
            return (f"claude pid {parent['pid']} session {str(s.get('sessionId', '?'))[:8]} "
                    f"'{s.get('name') or ''}' cwd {s.get('cwd') or '?'}")
        cur = parent
    parent = table.get(row["ppid"])
    return f"unknown (parent {row['ppid']}: {parent['args'][:60] if parent else 'gone'})"


def _proc_detail(env, pid):
    d = env.proc_root / str(pid)
    status = {}
    for ln in (d / "status").read_text().splitlines():
        k, _, v = ln.partition(":")
        status[k] = v.strip()
    threads = {"R": 0, "S": 0, "D": 0}
    for t in (d / "task").iterdir():
        try:
            st = _stat_fields((t / "stat").read_text())[1][0]
        except (OSError, IndexError):
            continue
        threads[st] = threads.get(st, 0) + 1
    return status.get("State", "?").split()[0], status, threads


def collect_processes(env, cpu=None):
    cpu = cpu or {}
    table, note = _ps_table(env)
    if table is None:
        return {"lines": [f"  {note}"], "procs": []}
    mine = [r for r in sorted(table.values(), key=lambda r: r["pid"]) if is_voice_mode_mcp(r["args"])]
    lines = [f"  {len(mine)} voice-mode MCP process(es)"]
    procs = []
    for r in mine:
        pc = f"{cpu[r['pid']]:.1f}% (last {TOP_SAMPLE_S:g} s)" if r["pid"] in cpu else f"{r['pcpu']:.1f}% (ps)"
        lines.append(f"  pid {r['pid']}  ppid {r['ppid']}  uid {r['uid']}  age {_age(r['age'])}  "
                     f"threads {r['nlwp'] if r['nlwp'] is not None else 'n/a'}  cpu {pc}")
        lines.append(f"    owner: {_owner(env, table, r)}")
        entry = {"pid": r["pid"], "uid": r["uid"], "state": None, "threads": {}}
        if env.system == "Linux":
            try:
                state, status, threads = _proc_detail(env, r["pid"])
                entry.update(state=state, threads=threads)
                order = ["R", "S", "D"] + sorted(k for k in threads if k not in ("R", "S", "D"))
                lines.append(f"    /proc: State {state}  voluntary {status.get('voluntary_ctxt_switches', '?')}  "
                             f"nonvoluntary {status.get('nonvoluntary_ctxt_switches', '?')}  thread states "
                             + ", ".join(f"{k} {threads[k]}" for k in order))
            except OSError as e:
                lines.append(f"    /proc: not readable ({type(e).__name__})")
        procs.append(entry)
    return {"lines": lines, "procs": procs}


# -- engines ------------------------------------------------------------------------------------------

def _env_file_values(env):
    out = {}
    try:
        for ln in (env.home / ".voicemode" / "voicemode.env").read_text().splitlines():
            k, sep, v = ln.partition("=")
            if sep and not k.strip().startswith("#"):
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def configured_ports(env):
    vals = _env_file_values(env)
    ports = set()
    for key in ("VOICEMODE_TTS_BASE_URLS", "VOICEMODE_STT_BASE_URLS"):
        for url in vals.get(key, "").split(","):
            u = urllib.parse.urlparse(url.strip())
            if u.hostname in LOCAL_HOSTS and u.port:
                ports.add(u.port)
    return ports


def silent_wav(seconds=1, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * rate * seconds)
    return buf.getvalue()


def _multipart(wav):
    b = "vmldiagnose7f3a"
    parts = []
    for name, value in (("model", "whisper-1"), ("response_format", "json")):
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="silence.wav"\r\n'
                 f"Content-Type: audio/wav\r\n\r\n".encode() + wav + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={b}"}


def _pick_voice(env, base):
    try:
        status, body = env.http("GET", base + "/v1/audio/voices", timeout=HEALTH_TIMEOUT_S)
        data = json.loads(body) if status == 200 else None
    except Exception:  # noqa: BLE001
        data = None
    items = (data.get("voices") or data.get("data") or []) if isinstance(data, dict) else (data or [])
    ids = []
    for v in items if isinstance(items, list) else []:
        vid = v if isinstance(v, str) else (v.get("id") or v.get("name") or v.get("voice")) if isinstance(v, dict) else None
        if isinstance(vid, str):
            ids.append(vid)
    return PROBE_VOICE if (PROBE_VOICE in ids or not ids) else ids[0]


def _listener(env, port):
    if env.system != "Linux":
        return ""
    out, _ = _cmd(env, ["ss", "-ltnpH", f"sport = :{port}"])
    m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', out or "")
    return f"  listener pid {m.group(2)} ({m.group(1)})" if m else ""


def _identity(body):
    try:
        data = json.loads(body)
    except ValueError:
        return body[:80].decode("utf-8", "replace") if isinstance(body, bytes) else ""
    if not isinstance(data, dict):
        return ""
    return " ".join(f"{k}={data[k]}" for k in ("engine", "provider", "model", "device", "backend", "status")
                    if k in data)


def collect_engines(env):
    configured = configured_ports(env)
    lines, engines = [], []
    for name, port, kind in env.ports:
        base = f"http://127.0.0.1:{port}"
        e = {"name": name, "port": port, "kind": kind, "configured": port in configured,
             "health_ok": False, "probe_s": None, "probe_error": None, "load_at": None}
        label = f"  {name} ({port}, {kind}, {'configured' if e['configured'] else 'not configured'})"
        try:
            status, body = env.http("GET", base + "/health", timeout=HEALTH_TIMEOUT_S)
            e["health_ok"] = status == 200
            ident = _identity(body)
            health = f"health {status} {ident}".rstrip()
        except Exception as ex:  # noqa: BLE001
            health = f"not answering ({type(ex).__name__}: {str(ex)[:80]})"
        lines.append(f"{label}: {health}{_listener(env, port)}")
        if not e["health_ok"]:
            e["probe_error"] = "health not answering"
            engines.append(e)
            continue
        try:
            if kind == "tts":
                voice = _pick_voice(env, base)
                req = json.dumps({"model": "tts-1", "input": PROBE_TEXT, "voice": voice,
                                  "response_format": "wav"}).encode()
                what = f"tts '{PROBE_TEXT}' voice {voice}"
                t0 = env.monotonic()
                status, _ = env.http("POST", base + "/v1/audio/speech", req,
                                     {"Content-Type": "application/json"}, timeout=PROBE_TIMEOUT_S)
            else:
                body, headers = _multipart(silent_wav())
                what = "stt 1 s of silence"
                t0 = env.monotonic()
                status, _ = env.http("POST", base + "/v1/audio/transcriptions", body, headers,
                                     timeout=PROBE_TIMEOUT_S)
            took = env.monotonic() - t0
            if status == 200:
                e["probe_s"] = round(took, 2)
            else:
                e["probe_error"] = f"HTTP {status}"
        except Exception as ex:  # noqa: BLE001
            e["probe_error"] = f"{type(ex).__name__}: {str(ex)[:80]}"
        e["load_at"] = _load1(env)
        result = f"{e['probe_s']:.2f} s" if e["probe_s"] is not None else f"failed: {e['probe_error']}"
        lines.append(f"    probe {what}: {result} (load {e['load_at']})")
        engines.append(e)
    return {"lines": lines, "engines": engines}


# -- audio path ---------------------------------------------------------------------------------------

def _devices(text):
    out = {}
    for ln in (text or "").splitlines():
        p = ln.split("\t")
        if len(p) >= 5 and p[0].strip().isdigit():
            out[p[0].strip()] = (p[1], p[-1].strip())
    return out


def _streams(text):
    out = []
    for ln in (text or "").splitlines():
        p = ln.split("\t")
        if len(p) >= 2 and p[0].strip().isdigit():
            out.append((p[0].strip(), p[1].strip()))
    return out


def streams_on_suspended(sinks, sources, sink_inputs, source_outputs):
    found = []
    for kind, dev_kind, streams, devs in (("sink-input", "sink", sink_inputs, _devices(sinks)),
                                          ("source-output", "source", source_outputs, _devices(sources))):
        for idx, dev in _streams(streams):
            name, state = devs.get(dev, ("?", "?"))
            if state == "SUSPENDED":
                found.append(f"{kind} {idx} on {dev_kind} {dev} {name} (SUSPENDED)")
    return found


def collect_audio(env):
    if env.system != "Linux":
        return {"lines": [f"  not collected on this OS ({env.system}): there is no pactl stream list here"],
                "streams_on_suspended": []}
    if not env.which("pactl"):
        return {"lines": ["  pactl not found: not collected"], "streams_on_suspended": []}
    lines = []
    info, note = _cmd(env, ["pactl", "info"])
    if info is None:
        lines.append(f"  {note}")
    else:
        lines += [f"  {ln.strip()}" for ln in info.splitlines()
                  if ln.split(":")[0].strip() in ("Server Name", "Server Version", "Default Sink", "Default Source")]
    got = {}
    for what in ("sinks", "sources", "sink-inputs", "source-outputs"):
        out, note = _cmd(env, ["pactl", "list", "short", what])
        got[what] = out
        if out is None:
            lines.append(f"  {note}")
    for what, kind in (("sinks", "sink"), ("sources", "source")):
        for idx, (name, state) in _devices(got[what]).items():
            lines.append(f"  {kind} {idx} {name}: {state}")
    for what in ("sink-inputs", "source-outputs"):
        if got[what] is not None:
            streams = _streams(got[what])
            lines.append(f"  {what}: {len(streams)}")
            lines += [f"    {ln.strip()}" for ln in got[what].splitlines() if ln.strip()]
    return {"lines": lines, "streams_on_suspended": streams_on_suspended(
        got["sinks"], got["sources"], got["sink-inputs"], got["source-outputs"])}


# -- exchanges log ------------------------------------------------------------------------------------

def parse_timing(text):
    return {k: float(v) for k, v in re.findall(r"([a-z_]+) ([0-9.]+)s", text or "")}


def _tail(path, nbytes):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        data = f.read()
    text = data.decode("utf-8", "replace")
    return text.splitlines()[1:] if size > nbytes else text.splitlines()


def _epoch(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def collect_exchanges(env):
    day = datetime.fromtimestamp(env.now()).strftime("%Y-%m-%d")
    path = env.home / ".voicemode" / "logs" / "conversations" / f"exchanges_{day}.jsonl"
    if not path.exists():
        return {"lines": [f"  no exchanges log for today ({path})"], "exchanges": []}
    rows = []
    for ln in _tail(path, EXCHANGE_TAIL_BYTES):
        try:
            rows.append(json.loads(ln))
        except ValueError:
            continue
    rows = [r for r in rows if isinstance(r, dict)][-EXCHANGE_ROWS:]
    exchanges = []
    for r in rows:
        meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
        t = parse_timing(meta.get("timing"))
        exchanges.append({"when": _epoch(r.get("timestamp")), "kind": r.get("type"),
                          "conv": r.get("conversation_id") or "?", "timing": meta.get("timing") or "",
                          "port": urllib.parse.urlparse(meta.get("provider_url") or "").port,
                          "ttfa": t.get("ttfa"), "gen": t.get("gen"), "stt": t.get("stt")})

    def slowness(x):
        vals = [v for v in (x["ttfa"], x["gen"], x["stt"]) if v is not None]
        return max(vals) if vals else -1.0
    ranked = sorted(range(len(exchanges)), key=lambda i: slowness(exchanges[i]), reverse=True)[:3]
    rank = {i: n + 1 for n, i in enumerate(ranked) if slowness(exchanges[i]) >= 0}
    lines = [f"  last {len(exchanges)} rows of {path.name} (timings only; no spoken text)"]
    for i, x in enumerate(exchanges):
        when = datetime.fromtimestamp(x["when"]).strftime("%H:%M:%S") if x["when"] else "??:??:??"
        mark = f"   <-- slowest #{rank[i]}" if i in rank else ""
        lines.append(f"  {when} {str(x['kind']):<3} {x['conv']} port {x['port'] or '?'}  {x['timing']}{mark}")
    return {"lines": lines, "exchanges": exchanges}


# -- debug log ----------------------------------------------------------------------------------------

def collect_debug_log(env):
    path = env.debug_log
    if not path.exists():
        return {"lines": [f"  no debug log at {path}"], "log_events": []}
    seen, picked = set(), []
    for ln in _tail(path, DEBUG_TAIL_BYTES):
        if LOG_PATTERN.search(ln) and not SPOKEN_TEXT.search(ln) and ln not in seen:
            seen.add(ln)
            picked.append(ln)
    picked = picked[-LOG_LINES:]
    events = []
    for ln in picked:
        m = LOG_STAMP.match(ln)
        when = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) if m else None
        events.append({"when": when, "line": ln})
    lines = [f"  last {len(picked)} matching lines of {path} (pattern: {LOG_PATTERN.pattern})"]
    lines += [f"  {ln[:300]}" for ln in picked]
    return {"lines": lines, "log_events": events}


# -- conch, py-spy -------------------------------------------------------------------------------------

def collect_conch(env):
    cli = env.voicemode_cli or env.which("voicemode")
    if not cli:
        return ["  voicemode CLI not found: not collected"]
    out, note = _cmd(env, [cli, "conch", "status"], CONCH_TIMEOUT_S)
    if out is None:
        return [f"  {note}"]
    return [f"  {ln}" for ln in out.splitlines() if ln.strip()] or ["  (no output)"]


def collect_pyspy(env, procs):
    exe = env.which("py-spy")
    if not exe:
        return ["  py-spy not installed: pip install py-spy"]
    lines, results, threads = [], {}, []

    def dump(pid):
        results[pid] = _cmd(env, [exe, "dump", "--nonblocking", "--pid", str(pid)], PYSPY_TIMEOUT_S)
    own = []
    for p in procs:
        if isinstance(p, dict) and env.uid is not None and p.get("uid") == env.uid:
            own.append(p["pid"])
        else:
            pid = p.get("pid") if isinstance(p, dict) else p
            lines.append(f"  {pid}: not dumped (another user's process)")
    for pid in own:   # at once: each may take up to its timeout
        t = threading.Thread(target=dump, args=(pid,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(PYSPY_TIMEOUT_S + 2)
    for pid in own:
        out, note = results.get(pid, (None, "did not finish"))
        lines.append(f"  --- pid {pid} ---")
        lines += [f"    {ln}" for ln in (out.splitlines() if out is not None else [note])]
    return lines


# -- Suspects -----------------------------------------------------------------------------------------

def suspects(f, now):
    out = []
    load = f.get("load") or {}
    if load.get("load1") is not None and load.get("cores"):
        if load["load1"] > LOAD_PER_CORE_SUSPECT * load["cores"]:
            out.append(f"load {load['load1']} > {LOAD_PER_CORE_SUSPECT} x {load['cores']} cores")
    for e in f.get("engines", []):
        if not e.get("configured"):
            continue
        tag = f"engine {e['name']} ({e['port']})"
        if not e.get("health_ok"):
            out.append(f"{tag}: configured, but its health check is not answering")
        elif e.get("probe_error"):
            out.append(f"{tag}: {e['kind']} probe failed: {e['probe_error']} (load {e.get('load_at')})")
        else:
            limit = TTS_PROBE_SLOW_S if e["kind"] == "tts" else STT_PROBE_SLOW_S
            if e.get("probe_s") is not None and e["probe_s"] > limit:
                out.append(f"{tag}: {e['kind']} probe {e['probe_s']} s > {limit} s (load {e.get('load_at')})")
    for s in f.get("streams_on_suspended", []):
        out.append(f"audio stream on a SUSPENDED device: {s}")
    for p in f.get("procs", []):
        if p.get("state") == "D" or (p.get("threads") or {}).get("D", 0) > 0:
            out.append(f"voice-mode pid {p['pid']}: thread state D (uninterruptible wait): "
                       f"{(p.get('threads') or {}).get('D', 0)} thread(s), process state {p.get('state')}")
    recent = [ev for ev in f.get("log_events", []) if ev.get("when") and now - ev["when"] <= LOG_WINDOW_S]
    pa = [ev for ev in recent if "PaErrorCode" in ev["line"]]
    if pa:
        out.append(f"{len(pa)} PaErrorCode line(s) in the last {LOG_WINDOW_S / 60:g} min of the debug log")
    fixed = [ev for ev in recent if "Falling back to fixed duration" in ev["line"]]
    if fixed:
        out.append(f"{len(fixed)} fixed-duration recording fallback(s) in the last {LOG_WINDOW_S / 60:g} min: "
                   "the mic then records its whole max duration without silence detection")
    for x in f.get("exchanges", []):
        if not x.get("when") or now - x["when"] > LOG_WINDOW_S:
            continue
        if x.get("ttfa") is not None and x["ttfa"] > EXCHANGE_TTFA_SUSPECT_S:
            out.append(f"exchange {x.get('conv', '')}: ttfa {x['ttfa']} s > {EXCHANGE_TTFA_SUSPECT_S} s ({x['timing']})")
        if x.get("stt") is not None and x["stt"] > EXCHANGE_STT_SUSPECT_S:
            out.append(f"exchange {x.get('conv', '')}: stt {x['stt']} s > {EXCHANGE_STT_SUSPECT_S} s ({x['timing']})")
    out.extend(_host_suspects(f.get("host")))
    return out


def _host_suspects(h):
    if not isinstance(h, dict):
        return []
    out = []
    load = _num(h.get("load"))
    if load is not None and load >= HOST_CPU_SUSPECT_PCT:
        out.append(f"Windows host CPU load {load:g}% >= {HOST_CPU_SUSPECT_PCT}% (WARN)")
    for p in h.get("top") or []:
        pct = _num(p.get("pct")) if isinstance(p, dict) else None
        name = str(p.get("name", "")) if isinstance(p, dict) else ""
        if pct is not None and pct > HOST_PROC_SUSPECT_PCT and name.lower() not in WSL_PROCESS_NAMES:
            out.append(f"Windows process {name} pid {p.get('pid')} uses {pct:.1f}% of the host "
                       f"> {HOST_PROC_SUSPECT_PCT}%")
    if h.get("ps_error") is None and h.get("services_collected", True):
        services = h.get("services") if isinstance(h.get("services"), dict) else {}
        for name in AUDIO_SERVICES:
            status = services.get(name)
            if status != "Running":
                out.append(f"Windows audio service {name} {status or 'not found'} (not Running)")
    ps_s = _num(h.get("ps_s"))
    if ps_s is not None and ps_s > POWERSHELL_SLOW_S:
        out.append(f"powershell.exe took {ps_s:.1f} s > {POWERSHELL_SLOW_S} s (a sign of host overload)")
    return out


# -- the report ---------------------------------------------------------------------------------------

def _step(fn, *args):
    try:
        res = fn(*args)
        return res if isinstance(res, dict) else {"lines": res}
    except Exception as e:  # noqa: BLE001 - a failed step is a line, never the end of the report
        return {"lines": [f"  failed: {type(e).__name__}: {str(e)[:200]}"]}


def _write(env, text):
    logs = env.home / ".voicemode" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(env.now()).strftime("%Y%m%d-%H%M%S")
    path, n = logs / f"diagnose-{stamp}.txt", 1
    while path.exists():
        n += 1
        path = logs / f"diagnose-{stamp}-{n}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def diagnose(env=None):
    """(text, suspects, path of the written report or None)."""
    env = env or Env.default()
    now = env.now()
    out = [f"voicemode-local stall diagnose  {datetime.fromtimestamp(now).isoformat(timespec='seconds')}  "
           f"{env.system}", "Read-only snapshot. Suspects are measured values over a threshold, not causes.", ""]
    facts = {}

    def section(title, res, *keys):
        out.append(f"== {title}")
        out.extend(res.get("lines", []))
        out.append("")
        for k in keys:
            if k in res:
                facts[k] = res[k]
    section("Load", _step(collect_load, env), "load")
    top = _step(collect_top, env)
    section("Top CPU", top)
    section("Windows host", _step(collect_windows_host, env), "host")
    procs = _step(collect_processes, env, top.get("cpu"))
    section("voice-mode MCP processes", procs, "procs")
    section("py-spy", _step(collect_pyspy, env, facts.get("procs", [])))
    section("Audio path", _step(collect_audio, env), "streams_on_suspended")
    section("Conch", _step(collect_conch, env))
    section("Engines", _step(collect_engines, env), "engines")
    section("Exchanges (today)", _step(collect_exchanges, env), "exchanges")
    section("Debug log", _step(collect_debug_log, env), "log_events")
    try:
        found = suspects(facts, now)
    except Exception as e:  # noqa: BLE001
        found = [f"(could not evaluate: {type(e).__name__}: {e})"]
    out.append("== Suspects")
    out.extend(f"  SUSPECT {s}" for s in found)
    if not found:
        out.append("  none: no measured value crossed a threshold")
    out.append(f"  thresholds: load > {LOAD_PER_CORE_SUSPECT} x cores; tts probe > {TTS_PROBE_SLOW_S} s; "
               f"stt probe > {STT_PROBE_SLOW_S} s; exchange ttfa > {EXCHANGE_TTFA_SUSPECT_S} s or "
               f"stt > {EXCHANGE_STT_SUSPECT_S} s; PaErrorCode / fixed-duration fallback / slow exchanges "
               f"within {LOG_WINDOW_S / 60:g} min; thread state D; a stream on a SUSPENDED device; "
               f"WSL only: Windows host CPU >= {HOST_CPU_SUSPECT_PCT}%, a non-WSL Windows process "
               f"> {HOST_PROC_SUSPECT_PCT}% of the host, an audio service not Running, "
               f"powershell.exe > {POWERSHELL_SLOW_S} s")
    text = "\n".join(out) + "\n"
    try:
        path = _write(env, text)
    except OSError as e:
        print(f"stall_diagnose: could not write the report: {e}", file=sys.stderr)
        path = None
    return text, found, path


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cli = None
    if "--voicemode-cli" in argv:
        i = argv.index("--voicemode-cli")
        cli = argv[i + 1] if i + 1 < len(argv) else None
    text, _, path = diagnose(Env.default(voicemode_cli=cli or None))
    sys.stdout.write(text)
    print(f"Report written to {path}" if path else "Report NOT written (see the error above)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
