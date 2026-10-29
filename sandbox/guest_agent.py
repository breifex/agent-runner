#!/usr/bin/env python3
"""Minimal guest agent for the Firecracker session executor.

Install this inside the dedicated guest rootfs and start it at boot. It has no
network dependency. The host speaks a length-framed JSON + tar protocol over
virtio-vsock. It replaces /work for every request, runs only fixed argv
patterns, bounds output, and returns a validated tar archive.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import struct
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

PORT = int(os.getenv("AGENT_GUEST_PORT", "1024"))
MAX_FRAME = 256 * 1024 * 1024
MAX_FILES = 500
MAX_FILE_BYTES = 20 * 1024 * 1024
ALLOWED = {
    ("python3", "-s", "/work/.sbx/code.py"),
    ("python3", "-s", "/work/.sbx/harness.py"),
    ("bash", "--noprofile", "--norc", "/work/.sbx/cmd.sh"),
}


def recv_exact(sock: socket.socket, n: int) -> bytes:
    parts = []
    while n:
        chunk = sock.recv(n)
        if not chunk:
            raise RuntimeError("peer closed connection")
        parts.append(chunk)
        n -= len(chunk)
    return b"".join(parts)


def recv_frame(sock: socket.socket) -> bytes:
    n = struct.unpack("!Q", recv_exact(sock, 8))[0]
    if n > MAX_FRAME:
        raise RuntimeError("frame too large")
    return recv_exact(sock, n)


def send_frame(sock: socket.socket, value: bytes) -> None:
    if len(value) > MAX_FRAME:
        raise RuntimeError("frame too large")
    sock.sendall(struct.pack("!Q", len(value)) + value)


def safe_extract(data: bytes, target: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        count = 0
        total = 0
        for member in archive:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise RuntimeError("unsafe input archive")
            count += 1
            total += member.size
            if count > MAX_FILES or total > MAX_FRAME or member.size > MAX_FILE_BYTES:
                raise RuntimeError("input archive exceeds limits")
        archive.fileobj.seek(0)
        archive.extractall(target, filter="data")


def safe_archive(source: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        count = 0
        total = 0
        for item in sorted(source.rglob("*")):
            if item.is_symlink() or not (item.is_file() or item.is_dir()):
                continue
            if item.is_file():
                size = item.stat().st_size
                total += size
                count += 1
                if count > MAX_FILES or total > MAX_FRAME or size > MAX_FILE_BYTES:
                    raise RuntimeError("output exceeds limits")
            archive.add(item, arcname=item.relative_to(source).as_posix(), recursive=False)
    return buf.getvalue()


def run_request(request: dict, archive: bytes) -> tuple[dict, bytes]:
    argv = request.get("argv")
    if not isinstance(argv, list) or tuple(argv) not in ALLOWED:
        raise RuntimeError("command not allowed")
    timeout = max(1, min(int(request.get("timeout", 60)), 300))
    work = Path("/work")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(mode=0o700)
    safe_extract(archive, work)
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/work",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "/work/.skills",
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": "/work/.sbx/mplconfig",
    }
    start = time.monotonic()
    try:
        completed = subprocess.run(argv, cwd="/work", env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=timeout, check=False)
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        completed = type("TimeoutResult", (), {"returncode": 124, "stdout": exc.stdout or b"", "stderr": exc.stderr or b""})()
        timed_out = True
    stdout = (completed.stdout or b"")[:2 * 1024 * 1024].decode("utf-8", "replace")
    stderr = (completed.stderr or b"")[:2 * 1024 * 1024].decode("utf-8", "replace")
    result = {"exit_code": int(completed.returncode), "stdout": stdout, "stderr": stderr,
              "timed_out": timed_out, "killed_reason": "", "duration_ms": int((time.monotonic() - start) * 1000)}
    return result, safe_archive(work)


def serve() -> None:
    if not hasattr(socket, "AF_VSOCK"):
        raise RuntimeError("guest kernel has no AF_VSOCK support")
    server = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((socket.VMADDR_CID_ANY, PORT))
    server.listen(8)
    while True:
        conn, _ = server.accept()
        try:
            request = json.loads(recv_frame(conn).decode("utf-8"))
            archive = recv_frame(conn)
            result, output = run_request(request, archive)
            send_frame(conn, json.dumps(result, separators=(",", ":")).encode())
            send_frame(conn, output)
        except Exception as exc:
            error = {"exit_code": 125, "stdout": "", "stderr": "guest agent rejected request", "timed_out": False, "killed_reason": str(exc)[:240]}
            try:
                send_frame(conn, json.dumps(error).encode())
                send_frame(conn, safe_archive(Path("/work")))
            except Exception:
                pass
        finally:
            conn.close()


if __name__ == "__main__":
    serve()
