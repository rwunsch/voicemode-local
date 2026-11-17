"""install.sh: who holds 8880 / 2022 decides what happens to it (lib/ports.sh).

install.sh used to run `kill "$(lsof -ti:8880)"` (Step 7a) and `kill "$(lsof -ti:2022)"`
(Step 7c, both STT branches): every listener was killed, whatever it was. Now, as in
voicemode-switch:

  this install's own earlier server     stopped by exact pid: a process of the current user
                                        whose command line names <install dir>/<server> and
                                        --port <port>
  a Docker container publishing the     `docker stop <its name>`, the exact name `docker ps
  port, named kokoro / whisper          --filter publish=<port>` reported
  anything else                         one line saying what holds the port, return 1, nothing
                                        killed (install.sh then exits non-zero)
  nothing                               return 0

lib/ports.sh is sourced with lsof, curl and docker stubbed. Every holder is a fake python
script inside the per-test tmp dir, started and stopped here by exact pid.
"""
import os
import signal
import stat
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PORTS_SH = REPO / "lib" / "ports.sh"
INSTALL_SH = REPO / "install.sh"


def _stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class Box:
    def __init__(self, tmp_path: Path, docker_ps: str = ""):
        self.tmp = tmp_path
        self.app, self.bin, self.scratch = tmp_path / "app", tmp_path / "bin", tmp_path / "scratch"
        for d in (self.app, self.bin, self.scratch):
            d.mkdir(parents=True, exist_ok=True)
        self.holders = []
        self.docker_log = self.scratch / "docker.log"
        (self.scratch / "docker_ps").write_text(docker_ps)
        _stub(self.bin / "docker", textwrap.dedent(f'''
            echo "$@" >> "{self.docker_log}"
            [ "$1" = ps ] && cat "{self.scratch}/docker_ps"
            exit 0
        '''))
        _stub(self.bin / "lsof", textwrap.dedent(f'''
            port="${{1#-ti:}}"
            pidf="{self.scratch}/holder_$port"
            [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null || exit 1
            cat "$pidf"
        '''))
        _stub(self.bin / "curl", "exit 7")

    def fake(self, name, *args, where="app"):
        d = self.app if where == "app" else self.tmp / where
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text("import time\ntime.sleep(60)\n")
        return ["python3", str(d / name), *args]

    def holder(self, port, cmd):
        out = subprocess.run(["bash", "-c", '"$@" >/dev/null 2>&1 </dev/null & echo $!', "x", *cmd],
                             capture_output=True, text=True, timeout=10)
        pid = int(out.stdout.strip())
        self.holders.append(pid)
        if port:
            (self.scratch / f"holder_{port}").write_text(str(pid))
        time.sleep(0.3)
        return pid

    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def free(self, port, kind):
        code = (f'set -euo pipefail\nSCRIPT_DIR="{self.app}"\n. "{PORTS_SH}"\n'
                f'if vm_free_port {port} "$(vm_{kind}_own_re {port})" {kind if kind == "kokoro" else "whisper"}; '
                f'then echo RC=0; else echo RC=1; fi\n')
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}"}
        return subprocess.run(["bash", "-c", code], capture_output=True, text=True, env=env, timeout=30)

    def read(self, p):
        return p.read_text() if p.exists() else ""

    def cleanup(self):
        for pid in self.holders:
            if self.alive(pid):
                os.kill(pid, signal.SIGKILL)


@pytest.fixture
def made(tmp_path):
    boxes = []

    def make(**kw):
        b = Box(tmp_path, **kw)
        boxes.append(b)
        return b

    yield make
    for b in boxes:
        b.cleanup()


def test_a_free_port_is_free(made):
    done = made().free(8880, "kokoro")
    assert "RC=0" in done.stdout, done.stderr
    assert done.stderr == ""


@pytest.mark.parametrize("port,kind,server", [
    (8880, "kokoro", "kokoro-onnx-server.py"),
    (2022, "stt", "faster-whisper-server.py"),
    (2022, "stt", "whisper-proxy.py"),
])
def test_our_own_earlier_server_is_stopped_by_exact_pid(made, port, kind, server):
    b = made()
    bystander = b.holder(0, b.fake(server, "--port", str(port), where="other-checkout"))
    ours = b.holder(port, b.fake(server, "--port", str(port)))
    done = b.free(port, kind)
    assert "RC=0" in done.stdout, done.stdout + done.stderr
    assert f"PID {ours}" in done.stdout + done.stderr
    assert not b.alive(ours)
    assert b.alive(bystander)
    assert "stop" not in b.read(b.docker_log)


def test_our_server_on_another_port_is_not_proof(made):
    b = made()
    pid = b.holder(8880, b.fake("kokoro-onnx-server.py", "--port", "8882"))
    done = b.free(8880, "kokoro")
    assert "RC=1" in done.stdout
    assert b.alive(pid)


@pytest.mark.parametrize("port,kind,ps", [
    (8880, "kokoro", "voicemode-kokoro\tghcr.io/remsky/kokoro-fastapi-cpu:latest\n"),
    (2022, "stt", "voicemode-whisper\tonerahmet/openai-whisper-asr-webservice:latest\n"),
])
def test_a_docker_container_of_ours_is_stopped_by_its_name(made, port, kind, ps):
    b = made(docker_ps=ps)
    proxy = b.holder(port, b.fake("docker-proxy-stand-in.py", where="elsewhere"))
    done = b.free(port, kind)
    assert "RC=0" in done.stdout, done.stdout + done.stderr
    name = ps.split("\t")[0]
    calls = b.read(b.docker_log).splitlines()
    assert any(c.startswith("ps ") and f"publish={port}" in c for c in calls)
    assert f"stop {name}" in calls
    assert b.alive(proxy)


def test_a_foreign_holder_is_named_and_not_killed(made):
    b = made()
    foreign = b.holder(8880, b.fake("someone-elses-server.py", "--port", "8880", where="elsewhere"))
    done = b.free(8880, "kokoro")
    assert "RC=1" in done.stdout
    out = done.stdout + done.stderr
    lines = [l for l in out.splitlines() if "8880 is held by" in l]
    assert len(lines) == 1 and f"PID {foreign}" in lines[0] and "not stopping it" in lines[0], out
    assert b.alive(foreign)


def test_another_docker_container_is_named_and_not_stopped(made):
    b = made(docker_ps="my-web\tnginx:latest\n")
    holder = b.holder(2022, b.fake("docker-proxy-stand-in.py", where="elsewhere"))
    done = b.free(2022, "stt")
    assert "RC=1" in done.stdout
    out = done.stdout + done.stderr
    assert "my-web" in out and "nginx:latest" in out and "not stopping it" in out
    assert "stop my-web" not in b.read(b.docker_log)
    assert b.alive(holder)


def test_install_sh_has_no_blind_kills_and_frees_both_ports():
    text = INSTALL_SH.read_text()
    assert 'kill "$(lsof' not in text
    assert '. "$SCRIPT_DIR/lib/ports.sh"' in text
    tail = text[text.index("# ─── Step 1: System packages"):]
    assert 'vm_free_port 8880 "$(vm_kokoro_own_re 8880)" kokoro || exit 1' in tail
    assert tail.count('vm_free_port 2022 "$(vm_stt_own_re 2022)" whisper || exit 1') == 2
