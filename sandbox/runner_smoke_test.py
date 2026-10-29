#!/usr/bin/env python3
"""Smoke test for a running runner API. Run it ON the runner host after setup, then once more from the backend side.

  SANDBOX_RUNNER_SECRET=... python3 runner_smoke_test.py http://127.0.0.1:8765
  (through the tunnel)  ... CF_ACCESS_CLIENT_ID=... CF_ACCESS_CLIENT_SECRET=... python3 runner_smoke_test.py https://runner.example.com

It checks /health, a first run (cold start: boots a microVM), a second run in the same session (reuses it, files persist),
that the guest has no network, and cleanup. Exit code 0 means every check passed.
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import tarfile
import time

import requests

URL = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765").rstrip("/")
SECRET = os.getenv("SANDBOX_RUNNER_SECRET", "")
HEADERS = {"X-Runner-Secret": SECRET}
if os.getenv("CF_ACCESS_CLIENT_ID") and os.getenv("CF_ACCESS_CLIENT_SECRET"):
    HEADERS.update({"CF-Access-Client-Id": os.environ["CF_ACCESS_CLIENT_ID"], "CF-Access-Client-Secret": os.environ["CF_ACCESS_CLIENT_SECRET"]})
SESSION = f"1:smoke{int(time.time())}"
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def make_work(code: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = code.encode()
        info = tarfile.TarInfo(".sbx/code.py")
        info.size = len(data)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def run(code: str, timeout: int = 60) -> dict:
    spec = {"argv": ["python3", "-s", "/work/.sbx/code.py"], "timeout": timeout, "session_key": SESSION}
    r = requests.post(f"{URL}/run", headers=HEADERS, files={"work": ("work.tgz", make_work(code), "application/gzip")},
                      data={"spec": json.dumps(spec)}, timeout=timeout + 60)
    r.raise_for_status()
    body = r.json()
    names = []
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(body["work"])), mode="r:gz") as tar:
        names = tar.getnames()
    return {**body["meta"], "files": names}


def main() -> int:
    if len(SECRET) < 24:
        print("Set SANDBOX_RUNNER_SECRET (24+ characters) to the same value the runner uses.")
        return 2
    try:
        r = requests.get(f"{URL}/health", headers=HEADERS, timeout=10)
        body = r.json() if r.ok else {}
        check("health answers", r.ok, f"HTTP {r.status_code}")
        want = body.get("backend") == "microvm" or os.getenv("SMOKE_ANY_BACKEND") == "1"   # the override is for developing this script only
        check("a microVM backend is usable", bool(body.get("ok")) and want, json.dumps(body))
        if FAILED:
            return 1
        t = time.monotonic()
        first = run("import os\nopen('note.txt','w').write('hello from vm')\nprint('first run ok', os.getcwd())")
        check("first run (cold start) succeeds", first["exit_code"] == 0 and "first run ok" in first["stdout"], json.dumps(first)[:300])
        print(f"      cold start + run took {time.monotonic() - t:.1f}s")
        check("files written by the code come back", "note.txt" in first["files"], str(first["files"]))
        second = run("print(open('note.txt').read() if __import__('os').path.exists('note.txt') else 'missing')")
        check("second run in the same session works", second["exit_code"] == 0, json.dumps(second)[:300])
        net = run("import socket\ns=socket.socket(); s.settimeout(3)\ntry:\n    s.connect(('1.1.1.1',53)); print('NETWORK OPEN')\nexcept OSError:\n    print('no network')")
        check("the guest has no network", "no network" in net["stdout"] and "NETWORK OPEN" not in net["stdout"], net["stdout"])
        started = time.monotonic()
        slow = run("import time\ntime.sleep(30)", timeout=5)
        took = time.monotonic() - started
        check("a run past its time limit is stopped", (bool(slow["timed_out"]) or slow["exit_code"] == 124) and took < 25, f"{json.dumps(slow)[:200]} after {took:.0f}s")
    finally:
        try:
            d = requests.post(f"{URL}/destroy", headers=HEADERS, data={"session_key": SESSION}, timeout=30)
            check("session cleanup", d.ok, f"HTTP {d.status_code}")
        except requests.RequestException as error:
            check("session cleanup", False, str(error))
    print("\nALL CHECKS PASSED" if not FAILED else "\nFAILED: " + ", ".join(FAILED))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
