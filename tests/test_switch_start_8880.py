"""`voicemode-switch start` with the CPU Kokoro ONNX on 8880 (KOKORO_ONNX=true, no GPU).

It used to run `kill "$(lsof -ti:8880)"`: every listener on 8880 was killed, whatever it was,
to make room for the ONNX server. Now who holds the port decides:

  this install's own earlier kokoro-onnx-server   stopped by exact pid (own_pids, anchored to
                                                  the install directory)
  a Docker container publishing 8880 whose image  `docker stop <its name>`, the exact name
  name contains "kokoro"                          `docker ps` reported
  anything else                                   one line saying what holds the port; start
                                                  exits non-zero and kills nothing
  nothing                                         the server starts, as before

Runs a copy of the script with /tmp redirected to a per-test scratch dir. lsof, curl and
docker are stubs; every holder is a fake python script inside the per-test tmp dir, started
and stopped here by exact pid.
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
SWITCH = REPO / "voicemode-switch"


def _stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class Box:
    def __init__(self, tmp_path: Path, docker_ps: str = ""):
        self.tmp = tmp_path
        self.root, self.home, self.bin, self.scratch = (tmp_path / n for n in ("app", "home", "bin", "scratch"))
        self.holders = []
        for d in (self.root / "lib", self.home / ".voicemode-local", self.home / ".voicemode", self.bin, self.scratch):
            d.mkdir(parents=True, exist_ok=True)
        self.script = self.root / "voicemode-switch"
        self.script.write_text(SWITCH.read_text().replace("/tmp/", f"{self.scratch}/"))
        self.script.chmod(0o755)
        (self.root / "lib/compute.sh").write_text((REPO / "lib/compute.sh").read_text())
        self.py_log = self.scratch / "python.log"
        self.docker_log = self.scratch / "docker.log"
        _stub(self.root / ".venv/bin/python", f'echo "$@" >> "{self.py_log}"; exit 0')
        _stub(self.root / "scripts/faster-whisper-start.sh", "exit 0")
        _stub(self.bin / "sleep", "exit 0")
        (self.scratch / "docker_ps").write_text(docker_ps)
        _stub(self.bin / "docker", textwrap.dedent(f'''
            echo "$@" >> "{self.docker_log}"
            [ "$1" = ps ] && cat "{self.scratch}/docker_ps"
            exit 0
        '''))
        # lsof -ti:8880 prints the holder's pid while it is alive.
        _stub(self.bin / "lsof", textwrap.dedent(f'''
            pidf="{self.scratch}/holder_8880"
            [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null || exit 1
            cat "$pidf"
        '''))
        _stub(self.bin / "curl", textwrap.dedent(f'''
            url="${{@: -1}}"
            case "$url" in
                http://127.0.0.1:8880/*)
                    pidf="{self.scratch}/holder_8880"
                    [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null && {{ echo '{{"status":"ok"}}'; exit 0; }} ;;
            esac
            exit 7
        '''))
        (self.home / ".voicemode-local/config").write_text("KOKORO_ONNX=true\n")
        (self.home / ".voicemode/voicemode.env").write_text("")

    def fake(self, name: str, *args, where: str = "app") -> list:
        d = self.root if where == "app" else self.tmp / where
        d.mkdir(parents=True, exist_ok=True)
        script = d / name
        script.write_text("import time\ntime.sleep(60)\n")
        return ["python3", str(script), *args]

    def holder(self, cmd: list, port_holder=True) -> int:
        out = subprocess.run(["bash", "-c", '"$@" >/dev/null 2>&1 </dev/null & echo $!', "x", *cmd],
                             capture_output=True, text=True, timeout=10)
        pid = int(out.stdout.strip())
        self.holders.append(pid)
        if port_holder:
            (self.scratch / "holder_8880").write_text(str(pid))
        time.sleep(0.3)
        return pid

    def alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def run(self, *args):
        env = {**os.environ, "HOME": str(self.home), "PATH": f"{self.bin}:{os.environ['PATH']}"}
        return subprocess.run([str(self.script), *args], env=env, capture_output=True, text=True, timeout=30)

    def read(self, path: Path) -> str:
        return path.read_text() if path.exists() else ""

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


def _onnx_started(b):
    deadline = time.time() + 5
    while time.time() < deadline:
        if "kokoro-onnx-server.py --port 8880" in b.read(b.py_log):
            return True
        time.sleep(0.05)
    return False


def test_our_own_earlier_server_is_stopped_by_exact_pid(made):
    b = made()
    bystander = b.holder(b.fake("kokoro-onnx-server.py", "--port", "8880", where="other-checkout"),
                         port_holder=False)
    ours = b.holder(b.fake("kokoro-onnx-server.py", "--port", "8880", "--model-dir", "m"))
    done = b.run("start")
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"PID {ours}" in done.stdout
    assert not b.alive(ours)
    assert b.alive(bystander)
    assert _onnx_started(b)
    assert "stop" not in b.read(b.docker_log)


def test_a_docker_kokoro_publishing_8880_is_stopped_by_its_name(made):
    b = made(docker_ps="voicemode-kokoro\tghcr.io/remsky/kokoro-fastapi-cpu:latest\n")
    proxy = b.holder(b.fake("docker-proxy-stand-in.py", where="elsewhere"))
    done = b.run("start")
    assert done.returncode == 0, done.stdout + done.stderr
    calls = b.read(b.docker_log).splitlines()
    assert any(c.startswith("ps ") and "publish=8880" in c for c in calls)
    assert "stop voicemode-kokoro" in calls
    assert b.alive(proxy)                      # nothing killed: Docker stops its own container
    assert "voicemode-kokoro" in done.stdout
    assert _onnx_started(b)


def test_a_foreign_holder_is_not_killed_and_start_exits_non_zero(made):
    b = made()
    foreign = b.holder(b.fake("someone-elses-server.py", "--port", "8880", where="elsewhere"))
    done = b.run("start")
    assert done.returncode != 0
    out = done.stdout + done.stderr
    lines = [l for l in out.splitlines() if "8880 is held by" in l]
    assert len(lines) == 1, out
    assert f"PID {foreign}" in lines[0] and "not stopping it" in lines[0]
    assert b.alive(foreign)
    assert not _onnx_started(b)


def test_a_docker_container_without_kokoro_in_its_image_is_not_stopped(made):
    b = made(docker_ps="my-web\tnginx:latest\n")
    holder = b.holder(b.fake("docker-proxy-stand-in.py", where="elsewhere"))
    done = b.run("start")
    assert done.returncode != 0
    out = done.stdout + done.stderr
    assert "my-web" in out and "nginx:latest" in out and "not stopping it" in out
    assert "stop my-web" not in b.read(b.docker_log)
    assert b.alive(holder)


def test_nothing_on_8880_starts_the_server_as_before(made):
    b = made()
    done = b.run("start")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "8880 is held by" not in done.stdout + done.stderr
    assert _onnx_started(b)
    assert "stop" not in b.read(b.docker_log)


def test_the_old_kill_everything_line_is_gone():
    text = SWITCH.read_text()
    assert 'kill "$(lsof -ti:8880)"' not in text
