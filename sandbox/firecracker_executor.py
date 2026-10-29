#!/usr/bin/env python3
"""Session-scoped Firecracker executor.

The web application invokes this narrow CLI. It owns one Firecracker VM per
opaque session key, transfers staged files over virtio-vsock, and never gives
the guest a host filesystem mount or network device.

Required runner-host environment:
  FIRECRACKER_BIN, FIRECRACKER_JAILER, FIRECRACKER_KERNEL,
  FIRECRACKER_ROOTFS, FIRECRACKER_RUN_ROOT

The rootfs must contain sandbox/guest_agent.py installed as a service listening
on AF_VSOCK port 1024. See install_guest_agent.sh.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

SESSION_RE = re.compile(r"^[0-9]+:[A-Za-z0-9_-]{1,100}$")
ALLOWED_ARGV = {
    ("python3", "-s", "/work/.sbx/code.py"),
    ("python3", "-s", "/work/.sbx/harness.py"),
    ("bash", "--noprofile", "--norc", "/work/.sbx/cmd.sh"),
}
PORT = int(os.getenv("FIRECRACKER_GUEST_PORT", "1024"))
GUEST_CID_MIN = 100
MAX_FRAME = 256 * 1024 * 1024
MAX_TIMEOUT = 300


def env_path(name: str, default: str) -> Path:
    return Path(os.getenv(name, default)).resolve()


FC = env_path("FIRECRACKER_BIN", "/usr/local/bin/firecracker")
JAILER = env_path("FIRECRACKER_JAILER", "/usr/local/bin/jailer")
KERNEL = env_path("FIRECRACKER_KERNEL", "/var/lib/agent-microvm/vmlinux")
BASE_ROOTFS = env_path("FIRECRACKER_ROOTFS", "/var/lib/agent-microvm/agent-rootfs.ext4")
RUN_ROOT = env_path("FIRECRACKER_RUN_ROOT", "/var/lib/agent-microvm/sessions")
USE_JAILER = os.getenv("FIRECRACKER_USE_JAILER", "1") == "1"
RUN_UID = int(os.getenv("FIRECRACKER_UID", "65534"))
RUN_GID = int(os.getenv("FIRECRACKER_GID", "65534"))


def fail(message: str, code: int = 1) -> "NoReturn":
    print(message, file=sys.stderr)
    raise SystemExit(code)


def session_dir(key: str) -> Path:
    if not SESSION_RE.fullmatch(key):
        fail("invalid session key")
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    return RUN_ROOT / digest


def vm_cid(key: str) -> int:
    value = int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big")
    return GUEST_CID_MIN + (value % 40000)


def api_request(api_socket: Path, method: str, path: str, payload: dict) -> dict:
    body = json.dumps(payload, separators=(",", ":")).encode()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(api_socket))
    request = (f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
               f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body
    sock.sendall(request)
    chunks = []
    while True:
        data = sock.recv(65536)
        if not data:
            break
        chunks.append(data)
    sock.close()
    raw = b"".join(chunks)
    head, _, response_body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1]) if len(head.split(b" ", 2)) > 1 else 500
    if status >= 300:
        raise RuntimeError(f"Firecracker API {path} returned HTTP {status}: {response_body[:500].decode(errors='replace')}")
    return json.loads(response_body) if response_body else {}


def send_frame(sock: socket.socket, value: bytes) -> None:
    if len(value) > MAX_FRAME:
        raise RuntimeError("frame is too large")
    sock.sendall(struct.pack("!Q", len(value)) + value)


def recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks = []
    while count:
        data = sock.recv(count)
        if not data:
            raise RuntimeError("guest agent closed the vsock")
        chunks.append(data)
        count -= len(data)
    return b"".join(chunks)


def recv_frame(sock: socket.socket) -> bytes:
    size = struct.unpack("!Q", recv_exact(sock, 8))[0]
    if size > MAX_FRAME:
        raise RuntimeError("guest response is too large")
    return recv_exact(sock, size)


def connect_vsock(uds: Path, port: int, timeout: float) -> socket.socket:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(max(1.0, deadline - time.monotonic()))
            sock.connect(str(uds))
            sock.sendall(f"CONNECT {port}\n".encode())
            ack = b""
            while not ack.endswith(b"\n") and len(ack) < 80:
                ack += sock.recv(80 - len(ack))
            if not ack.startswith(b"OK "):
                raise RuntimeError(f"vsock connection rejected: {ack!r}")
            return sock
        except (OSError, RuntimeError) as exc:
            last = exc
            try:
                sock.close()
            except Exception:
                pass
            time.sleep(0.1)
    raise RuntimeError(f"guest agent did not become ready: {last}")


def tar_bytes(directory: Path) -> bytes:
    with tempfile.NamedTemporaryFile(prefix="agent-input-", suffix=".tgz") as tmp:
        with tarfile.open(fileobj=tmp, mode="w:gz") as archive:
            for item in sorted(directory.rglob("*")):
                if item.is_symlink() or not (item.is_file() or item.is_dir()):
                    continue
                archive.add(item, arcname=item.relative_to(directory).as_posix(), recursive=False)
        tmp.flush()
        tmp.seek(0)
        return tmp.read()


def untar_bytes(data: bytes, directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tarfile.open(fileobj=__import__("io").BytesIO(data), mode="r:gz") as archive:
        total = 0
        for member in archive:
            name = Path(member.name)
            if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
                raise RuntimeError("guest returned unsafe archive entry")
            total += member.size
            if total > MAX_FRAME:
                raise RuntimeError("guest returned too much data")
        archive.fileobj.seek(0)
        archive.extractall(directory, filter="data")


def write_private(path: Path, text: str, mode: int = 0o600) -> None:
    """Write a small file that is never readable by other users (Path.write_text has no mode argument)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, mode)


def jail_dir(state: Path) -> Path:
    """The jailer builds its chroot at <chroot-base-dir>/<exec file name>/<id>/root (see Firecracker docs/jailer.md)."""
    return RUN_ROOT / FC.name / state.name


def socket_paths(state: Path) -> tuple[Path, Path]:
    """(API socket, vsock socket). Under the jailer both live inside the VM's chroot."""
    if USE_JAILER:
        root = jail_dir(state) / "root"
        return root / "run" / "firecracker.socket", root / "vsock.sock"
    return state / "api.sock", state / "vsock.sock"


def pid_alive(path: Path) -> bool:
    try:
        pid = int(path.read_text())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def start_vm(key: str) -> Path:
    state = session_dir(key)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_file = state / "session.key"
    if key_file.exists() and key_file.read_text() != key:
        raise RuntimeError("session state identity mismatch")
    if not key_file.exists():
        key_file.write_text(key)
        os.chmod(key_file, 0o600)
    pid_file = state / "firecracker.pid"
    api, vsock = socket_paths(state)
    if pid_file.exists() and pid_alive(pid_file) and api.exists() and vsock.exists():
        return state
    for item in (pid_file, api, vsock):
        item.unlink(missing_ok=True)
    if not FC.is_file() or not KERNEL.is_file() or not BASE_ROOTFS.is_file():
        raise RuntimeError("Firecracker, kernel, or rootfs is not installed")
    guest_rootfs = state / "rootfs.ext4"
    if not guest_rootfs.exists():
        shutil.copyfile(BASE_ROOTFS, guest_rootfs)
        os.chmod(guest_rootfs, 0o600)
    cid = vm_cid(key)
    config = state / "config.json"
    write_private(config, json.dumps({
        "boot-source": {"kernel_image_path": str(KERNEL), "boot_args": "console=ttyS0 reboot=k panic=1 init=/sbin/init"},
        "drives": [{"drive_id": "rootfs", "path_on_host": str(guest_rootfs), "is_root_device": True, "is_read_only": False}],
        "machine-config": {"vcpu_count": 1, "mem_size_mib": int(os.getenv("FIRECRACKER_MEM_MIB", "512")), "smt": False},
        "vsock": {"guest_cid": cid, "uds_path": str(vsock)},
    }))
    if USE_JAILER:
        if not JAILER.is_file():
            raise RuntimeError("FIRECRACKER_USE_JAILER=1 but jailer is not installed")
        # The jailer needs root (chroot, mount namespace, chown), so this executor runs as root on the dedicated runner host.
        if os.geteuid() != 0:
            raise RuntimeError("FIRECRACKER_USE_JAILER=1 needs the executor to run as root (the jailer drops to FIRECRACKER_UID afterwards)")
        command = [str(JAILER), "--id", state.name, "--exec-file", str(FC),
                   "--chroot-base-dir", str(RUN_ROOT), "--uid", str(RUN_UID), "--gid", str(RUN_GID),
                   "--", "--api-sock", "/run/firecracker.socket", "--config-file", "/config.json"]
        # Stage the VM's files inside the jail before starting. Firecracker runs as RUN_UID inside the chroot, so it must own them.
        jail_root = jail_dir(state) / "root"
        shutil.rmtree(jail_dir(state), ignore_errors=True)   # a leftover jail from a dead VM
        jail_root.mkdir(mode=0o700, parents=True)
        config_data = json.loads(config.read_text())
        config_data["boot-source"]["kernel_image_path"] = "/kernel"
        config_data["drives"][0]["path_on_host"] = "/rootfs.ext4"
        config_data["vsock"]["uds_path"] = "/vsock.sock"
        shutil.copyfile(KERNEL, jail_root / "kernel")
        try:
            os.link(guest_rootfs, jail_root / "rootfs.ext4")      # same disk: no second copy, and changes persist for the session
        except OSError:
            shutil.copyfile(guest_rootfs, jail_root / "rootfs.ext4")
        write_private(jail_root / "config.json", json.dumps(config_data), 0o400)
        for item, mode in (("kernel", 0o400), ("rootfs.ext4", 0o600), ("config.json", 0o400)):
            os.chmod(jail_root / item, mode)
            os.chown(jail_root / item, RUN_UID, RUN_GID)
        (jail_root / "run").mkdir(mode=0o700, exist_ok=True)   # Firecracker creates its API socket here
        os.chown(jail_root / "run", RUN_UID, RUN_GID)
        os.chown(jail_root, RUN_UID, RUN_GID)
    else:
        command = [str(FC), "--api-sock", str(api), "--config-file", str(config)]
    sink: object = subprocess.DEVNULL
    if os.getenv("FIRECRACKER_DEBUG_LOG") == "1":   # guest console + Firecracker messages, for diagnosing a VM that will not boot
        sink = open(os.open(state / "firecracker.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w")
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=sink, stderr=sink, start_new_session=True, close_fds=True)
    if sink is not subprocess.DEVNULL:
        sink.close()
    write_private(pid_file, str(proc.pid))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if api.exists() and vsock.exists():
            return state
        if proc.poll() is not None:
            raise RuntimeError("Firecracker exited during startup")
        time.sleep(0.05)
    raise RuntimeError("Firecracker API socket did not become ready")


def run_session(key: str, work: Path, argv: list[str], timeout: int) -> dict:
    if tuple(argv) not in ALLOWED_ARGV:
        raise RuntimeError("command is not in the server allowlist")
    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    state = start_vm(key)
    (state / "last_used").touch(mode=0o600)
    # In jailer mode the sockets are inside the jail root; start_vm returns the
    # public state path but its socket paths are discoverable from the layout.
    _, vsock = socket_paths(state)
    sock = connect_vsock(vsock, PORT, 15)
    try:
        payload = tar_bytes(work)
        request = {"op": "run", "argv": argv, "timeout": timeout, "max_output": 2 * 1024 * 1024}
        send_frame(sock, json.dumps(request, separators=(",", ":")).encode())
        send_frame(sock, payload)
        response = json.loads(recv_frame(sock).decode("utf-8"))
        output_archive = recv_frame(sock)
        untar_bytes(output_archive, work)
        return response
    finally:
        sock.close()


def destroy(key: str) -> None:
    state = session_dir(key)
    if not state.exists():
        return
    pid_file = state / "firecracker.pid"
    try:
        pid = int(pid_file.read_text())
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
    except (OSError, ValueError):
        pass
    shutil.rmtree(state, ignore_errors=True)
    shutil.rmtree(jail_dir(state), ignore_errors=True)


def health() -> int:
    checks = {"firecracker": FC.is_file() and os.access(FC, os.X_OK), "jailer": (not USE_JAILER) or (JAILER.is_file() and os.access(JAILER, os.X_OK)), "kernel": KERNEL.is_file(), "rootfs": BASE_ROOTFS.is_file(), "kvm": os.access("/dev/kvm", os.R_OK | os.W_OK)}
    print(json.dumps({"ok": all(checks.values()), "backend": "microvm", "checks": checks}))
    return 0 if all(checks.values()) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--destroy", action="store_true")
    parser.add_argument("--session-key")
    parser.add_argument("--work")
    parser.add_argument("--argv-json")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    if args.health:
        return health()
    if not args.session_key:
        fail("--session-key is required")
    if args.destroy:
        destroy(args.session_key)
        return 0
    if not args.work or not args.argv_json:
        fail("--work and --argv-json are required")
    try:
        argv = json.loads(args.argv_json)
        result = run_session(args.session_key, Path(args.work).resolve(), argv, args.timeout)
        print(json.dumps(result, separators=(",", ":")))
        return 0 if result.get("exit_code") == 0 and not result.get("timed_out") else 1
    except Exception as exc:  # safe CLI error; details go only to runner stderr
        print(f"microVM execution failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
