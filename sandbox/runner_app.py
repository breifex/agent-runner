"""Stand-alone microVM runner. Deploy this on a separate locked-down runner host.
The host must provide a dedicated Firecracker-compatible SANDBOX_MICROVM_EXECUTOR;
the web server never receives access to that executor or to the host runtime."""
from __future__ import annotations

import asyncio
import base64
import hmac
import os
import re
import shutil

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

from sandbox.backends import RunSpec, get_backend
from sandbox.policy import LIMITS
from sandbox.staging import new_stage, tar_dir, untar_to

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_slots: asyncio.Semaphore | None = None   # created lazily inside the running event loop
ALLOWED_ARGV = [
    ["python3", "-s", "/work/.sbx/code.py"],
    ["python3", "-s", "/work/.sbx/harness.py"],
    ["bash", "--noprofile", "--norc", "/work/.sbx/cmd.sh"],
]
SESSION_KEY = re.compile(r"^[0-9]+:[A-Za-z0-9_-]{1,100}$")


def _auth(secret: str | None) -> None:
    expected = os.getenv("SANDBOX_RUNNER_SECRET", "")
    if len(expected) < 24 or not secret or not hmac.compare_digest(secret.encode(), expected.encode()):
        raise HTTPException(status_code=404, detail="Not Found")


def _max_parallel() -> int:
    try:
        return max(1, min(64, int(os.getenv("RUNNER_MAX_PARALLEL", "") or 4)))
    except ValueError:
        return 4


def _execute(backend, data: bytes, argv: list, timeout: int, session_key: str) -> dict:
    """Blocking work (unpack, run the VM, repack). It runs in a worker thread so /health keeps answering during a long run."""
    stage = new_stage()
    try:
        target = stage / "work"
        target.mkdir()
        untar_to(data, target, LIMITS.max_work_bytes)
        raw = backend.run(RunSpec(argv=argv, timeout=timeout, work=target, session_key=session_key), lambda: "")
        return {"meta": {"exit_code": raw.exit_code, "stdout": raw.stdout, "stderr": raw.stderr, "timed_out": raw.timed_out, "killed_reason": raw.killed_reason},
                "work": base64.b64encode(tar_dir(target)).decode()}
    finally:
        shutil.rmtree(stage, ignore_errors=True)


@app.get("/health")
def health(x_runner_secret: str | None = Header(default=None)) -> dict:
    _auth(x_runner_secret)
    backend, ok, detail = get_backend()
    return {"ok": ok, "backend": backend.name, "detail": detail}


@app.post("/run")
async def run(work: UploadFile = File(...), spec: str = Form(...), x_runner_secret: str | None = Header(default=None)) -> dict:
    import json

    _auth(x_runner_secret)
    backend, ok, _ = get_backend()
    if not ok or backend.remote:
        raise HTTPException(status_code=503, detail="No sandbox on this runner")
    try:
        parsed = json.loads(spec)
        argv, timeout = parsed["argv"], max(1, min(int(parsed["timeout"]), LIMITS.max_timeout))
        session_key = str(parsed["session_key"])
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(status_code=422, detail="Bad request") from error
    if argv not in ALLOWED_ARGV:
        raise HTTPException(status_code=422, detail="That command is not allowed")
    if not SESSION_KEY.fullmatch(session_key):
        raise HTTPException(status_code=422, detail="Invalid session identity")
    data = await work.read(LIMITS.max_work_bytes)
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(_max_parallel())
    async with _slots:   # at most RUNNER_MAX_PARALLEL microVM runs at once; the rest wait their turn
        return await run_in_threadpool(_execute, backend, data, argv, timeout, session_key)


@app.post("/destroy")
def destroy(session_key: str = Form(...), x_runner_secret: str | None = Header(default=None)) -> dict:
    _auth(x_runner_secret)
    if not SESSION_KEY.fullmatch(session_key):
        raise HTTPException(status_code=422, detail="Invalid session identity")
    backend, ok, _ = get_backend()
    if not ok or backend.remote:
        raise HTTPException(status_code=503, detail="No microVM sandbox on this runner")
    backend.destroy_session(session_key)
    return {"ok": True}
