#!/usr/bin/env python3
"""Measure TTS behaviour under concurrent sessions.

The failure this exists to detect is not slowness in the average case. It is the one recorded in
this repo's CLAUDE.md: under several sessions speaking at once, CPU synthesis saturates the cores
the real-time audio pipeline needs, and sentences drop mid-word. A single-request benchmark cannot
see it, which is why the GPU path was adopted here in the first place.

So this fires N requests simultaneously and reports the SLOWEST one alongside the median, plus the
system CPU load while they run. The slowest request is the one a listener actually notices.

Usage:
    python3 tests/concurrency_bench.py --engines onnx=8882 gpu=8880 --levels 1 2 4 8
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import urllib.request

SENTENCE = (
    "The activity has three open tickets, and the customer has not replied since Tuesday."
)


def synth(port: int, voice: str, timeout: float) -> tuple[float, int, int]:
    """One synthesis request. Returns (seconds, http status, bytes)."""
    payload = json.dumps(
        {
            "model": "kokoro",
            "input": SENTENCE,
            "voice": voice,
            "response_format": "wav",
        }
    ).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/audio/speech",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return time.perf_counter() - start, resp.status, len(body)
    except Exception:
        return time.perf_counter() - start, 0, 0


def cpu_sample() -> float:
    """System-wide CPU busy fraction over a 0.5s window, from /proc/stat."""

    def read() -> tuple[int, int]:
        with open("/proc/stat") as fh:
            parts = [int(x) for x in fh.readline().split()[1:]]
        idle = parts[3] + parts[4]
        return sum(parts), idle

    total0, idle0 = read()
    time.sleep(0.5)
    total1, idle1 = read()
    dt, di = total1 - total0, idle1 - idle0
    return 0.0 if dt <= 0 else (1 - di / dt) * 100


def run_level(port: int, n: int, voice: str, timeout: float) -> dict:
    results: list[tuple[float, int, int]] = []
    lock = threading.Lock()
    peak_cpu = 0.0

    def worker() -> None:
        r = synth(port, voice, timeout)
        with lock:
            results.append(r)

    def watcher(stop: threading.Event) -> None:
        nonlocal peak_cpu
        while not stop.is_set():
            peak_cpu = max(peak_cpu, cpu_sample())

    stop = threading.Event()
    w = threading.Thread(target=watcher, args=(stop,), daemon=True)
    w.start()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    wall0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - wall0

    stop.set()
    w.join(timeout=2)

    times = [r[0] for r in results]
    failures = sum(1 for r in results if r[1] != 200)
    return {
        "n": n,
        "wall": wall,
        "median": statistics.median(times),
        "slowest": max(times),
        "failures": failures,
        "peak_cpu": peak_cpu,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engines", nargs="+", required=True, help="name=port, e.g. onnx=8882")
    ap.add_argument("--levels", nargs="+", type=int, default=[1, 2, 4, 8])
    ap.add_argument("--voice", default="af_sky")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()

    engines = []
    for spec in args.engines:
        name, _, port = spec.partition("=")
        engines.append((name, int(port)))

    print(f"sentence: {len(SENTENCE)} chars, voice {args.voice}")
    print(f"{'engine':<8} {'N':>3} {'wall':>8} {'median':>8} {'SLOWEST':>9} {'fails':>6} {'peak CPU':>9}")
    print("-" * 56)
    for name, port in engines:
        for n in args.levels:
            r = run_level(port, n, args.voice, args.timeout)
            print(
                f"{name:<8} {r['n']:>3} {r['wall']:>7.2f}s {r['median']:>7.2f}s "
                f"{r['slowest']:>8.2f}s {r['failures']:>6} {r['peak_cpu']:>8.0f}%"
            )
            time.sleep(2)  # let the machine settle between levels


if __name__ == "__main__":
    main()
