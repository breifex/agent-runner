"""Where code actually runs.
The web application never runs user commands directly. Production can explicitly
select a dedicated microVM runner or the separately acknowledged Docker backend;
unconfigured or unhealthy backends fail closed without an unsafe fallback."""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sandbox.policy import LIMITS

IMAGE = os.getenv("SANDBOX_IMAGE", "agent-sandbox:latest")


@dataclass
class RunSpec:
    argv: list[str]                 # command to run inside, paths under /work
    timeout: int
    work: Path                      # host directory that appears as /work
    session_key: str = ""            # opaque, stable user/session sandbox identity
    name: str = field(default_factory=lambda: "sbx-" + uuid.uuid4().hex[:12])


@dataclass
class RawResult:
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    killed_reason: str = ""


def _cap(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    return text if len(text) <= LIMITS.output_cap else text[: LIMITS.output_cap] + "\n[output cut]"


def run_capped(argv: list[str], timeout: float, watch: Callable[[], str], kill: Callable[[], None], cwd: Path | None = None, env: dict | None = None) -> RawResult:
    """Run a process, keep at most output_cap bytes of output, enforce the time limit, and let `watch` stop it early."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, cwd=cwd, env=env, start_new_session=True)
    bufs: dict[str, bytearray] = {"out": bytearray(), "err": bytearray()}

    def drain(stream, key: str) -> None:
        cap = LIMITS.output_cap * 4
        for chunk in iter(lambda: stream.read(8192), b""):
            if len(bufs[key]) < cap:
                bufs[key] += chunk[: cap - len(bufs[key])]

    readers = [threading.Thread(target=drain, args=(proc.stdout, "out"), daemon=True), threading.Thread(target=drain, args=(proc.stderr, "err"), daemon=True)]
    for t in readers:
        t.start()
    result = RawResult()
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if time.monotonic() > deadline:
            result.timed_out = True
        else:
            result.killed_reason = watch()
        if result.timed_out or result.killed_reason:
            kill()
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            break
        time.sleep(0.25)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    for t in readers:
        t.join(timeout=2)
    for stream in (proc.stdout, proc.stderr):
        try:
            stream.close()
        except OSError:
            pass
    result.exit_code = proc.returncode if proc.returncode is not None else -1
    result.stdout, result.stderr = _cap(bytes(bufs["out"])), _cap(bytes(bufs["err"]))
    return result


class Backend:
    name = "none"
    remote = False

    def status(self) -> tuple[bool, str]:
        return False, "Code execution is turned off: no sandbox is configured."

    def run(self, spec: RunSpec, watch: Callable[[], str]) -> RawResult:
        raise RuntimeError("No sandbox available")

    def destroy_session(self, session_key: str) -> None:
        """Destroy a session VM when this backend owns one; unavailable is a no-op."""
        return None


def _timeout_wrap(spec: RunSpec) -> list[str]:
    return ["timeout", "-k", "2", str(spec.timeout), *spec.argv]


class MicroVMHostBackend(Backend):
    """Executes one run through a dedicated microVM executor binary.

    The binary is an intentionally narrow host boundary: it must create or reuse
    only the VM identified by the session key, attach only that session's staged
    work directory, disable networking, apply limits, and return stdout/stderr.
    The application never invokes a user-provided command directly.
    """
    name = "microvm"
    def __init__(self) -> None:
        self.executor = os.getenv("SANDBOX_MICROVM_EXECUTOR", "/usr/local/bin/agent-microvm-exec")

    @staticmethod
    def _executor_env() -> dict[str, str]:
        allowed = ("FIRECRACKER_BIN", "FIRECRACKER_JAILER", "FIRECRACKER_KERNEL", "FIRECRACKER_ROOTFS",
                   "FIRECRACKER_RUN_ROOT", "FIRECRACKER_USE_JAILER", "FIRECRACKER_MEM_MIB", "FIRECRACKER_GUEST_PORT",
                   "FIRECRACKER_UID", "FIRECRACKER_GID", "FIRECRACKER_DEBUG_LOG")
        return {"PATH": "/usr/bin:/bin:/usr/local/bin", **{key: os.environ[key] for key in allowed if key in os.environ}}

    def status(self) -> tuple[bool, str]:
        if not os.path.isfile(self.executor) or not os.access(self.executor, os.X_OK):
            return False, "The dedicated microVM executor is not installed."
        try:
            probe = subprocess.run([self.executor, "--health"], capture_output=True, timeout=8, env=self._executor_env())
        except (subprocess.TimeoutExpired, OSError):
            return False, "The microVM executor did not respond."
        return (True, "microVM executor") if probe.returncode == 0 else (False, "The microVM executor is unhealthy.")
    def run(self, spec: RunSpec, watch: Callable[[], str]) -> RawResult:
        import json
        argv = [self.executor, "--session-key", spec.session_key, "--work", str(spec.work), "--argv-json", json.dumps(spec.argv), "--timeout", str(spec.timeout)]
        raw = run_capped(argv, spec.timeout + 20, watch, lambda: None, env=self._executor_env())
        # The executor prints the guest's result as one JSON object; unpack it so stdout/exit code/timeout are the program's, not the executor's.
        try:
            guest = json.loads(raw.stdout)
        except ValueError:
            guest = None
        if isinstance(guest, dict) and "exit_code" in guest:
            return RawResult(exit_code=int(guest.get("exit_code", -1)), stdout=_cap(str(guest.get("stdout", "")).encode()), stderr=_cap(str(guest.get("stderr", "")).encode()),
                             timed_out=bool(guest.get("timed_out")) or raw.timed_out, killed_reason=str(guest.get("killed_reason", "")) or raw.killed_reason)
        # No answer from the guest (VM did not start, vsock failed, ...): keep the details in the runner's log, not in the model's output.
        import logging
        logging.getLogger(__name__).warning("microVM executor failed (exit %s): %s", raw.exit_code, raw.stderr[-500:])
        return RawResult(exit_code=raw.exit_code if raw.exit_code else -1, stdout="", stderr="The sandbox could not run this code. Please try again.",
                         timed_out=raw.timed_out, killed_reason=raw.killed_reason)

    def destroy_session(self, session_key: str) -> None:
        subprocess.run([self.executor, "--destroy", "--session-key", session_key], capture_output=True, timeout=20, env=self._executor_env())

class InsecureTestBackend(Backend):
    """For automated tests ONLY: runs the command directly on this machine. Refused unless explicitly allowed outside production."""
    name = "insecure-test"

    def status(self) -> tuple[bool, str]:
        if os.getenv("SANDBOX_ALLOW_INSECURE_TEST") == "1" and os.getenv("APP_ENV", "").lower() not in ("production", "prod"):
            return True, "INSECURE test backend (no isolation)"
        return False, "The insecure test backend is not allowed here."

    def run(self, spec: RunSpec, watch: Callable[[], str]) -> RawResult:
        import site
        argv = [a.replace("/work", str(spec.work)) for a in _timeout_wrap(spec)]
        user_site = site.getusersitepackages()
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(spec.work), "PYTHONPATH": os.pathsep.join((str(spec.work / ".skills"), user_site)), "MPLBACKEND": "Agg", "MPLCONFIGDIR": str(spec.work / ".sbx")}
        return run_capped(argv, spec.timeout + 15, watch, lambda: None, cwd=spec.work, env=env)


class DockerHostBackend(Backend):
    """Runs each tool call in a one-shot, network-disabled Docker container on this host.

    This requires a local Docker Engine and a prebuilt sandbox image. Docker daemon
    access is highly privileged, so this backend must run only on a dedicated host.
    It is a weaker tenant boundary than Firecracker because containers share a kernel.
    """
    name = "docker"
    ALLOWED_ARGV = (
        ["python3", "-s", "/work/.sbx/code.py"],
        ["python3", "-s", "/work/.sbx/harness.py"],
        ["bash", "--noprofile", "--norc", "/work/.sbx/cmd.sh"],
    )

    def __init__(self) -> None:
        self.cli = os.getenv("SANDBOX_DOCKER_CLI", "docker")
        self.socket = os.getenv("SANDBOX_DOCKER_SOCKET", "/var/run/docker.sock")
        self.image = os.getenv("SANDBOX_DOCKER_IMAGE", IMAGE)
        self.runtime = os.getenv("SANDBOX_DOCKER_RUNTIME", "").strip()

    def _docker(self, *args: str) -> list[str]:
        return [self.cli, "--host", f"unix://{self.socket}", *args]

    @staticmethod
    def _allowed_in_production() -> bool:
        return (os.getenv("APP_ENV", "production").lower() not in ("production", "prod")
                or os.getenv("SANDBOX_DOCKER_ALLOW_PRODUCTION") == "1")

    def status(self) -> tuple[bool, str]:
        if not self._allowed_in_production():
            return False, "Docker sandboxing in production requires SANDBOX_DOCKER_ALLOW_PRODUCTION=1 after reviewing its shared-kernel isolation risk."
        if not Path(self.socket).is_absolute() or ".." in Path(self.socket).parts:
            return False, "SANDBOX_DOCKER_SOCKET must be an absolute local Unix-socket path."
        if self.runtime and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", self.runtime):
            return False, "SANDBOX_DOCKER_RUNTIME contains unsupported characters."
        try:
            daemon = subprocess.run(self._docker("info", "--format", "{{.ServerVersion}}"), capture_output=True, timeout=8)
        except (subprocess.TimeoutExpired, OSError):
            return False, "The local Docker Engine is unavailable."
        if daemon.returncode != 0:
            return False, "The backend cannot connect to the local Docker Engine."
        try:
            image = subprocess.run(self._docker("image", "inspect", self.image), capture_output=True, timeout=8)
        except (subprocess.TimeoutExpired, OSError):
            return False, "The configured Docker sandbox image could not be checked."
        if image.returncode != 0:
            return False, f"Docker sandbox image '{self.image}' is not available locally; build it before enabling code execution."
        return True, f"local Docker sandbox ({self.image})"

    def run(self, spec: RunSpec, watch: Callable[[], str]) -> RawResult:
        if spec.argv not in self.ALLOWED_ARGV:
            raise RuntimeError("Docker sandbox command is not allowed")
        if not spec.work.is_dir():
            raise RuntimeError("Docker sandbox work directory is unavailable")
        name = "agent-sbx-" + uuid.uuid4().hex[:20]
        work = str(spec.work.resolve(strict=True))
        timeout = max(1, min(int(spec.timeout), LIMITS.max_timeout))
        create = self._docker(
            "create", "--name", name, "--label", "agent-sandbox=true", "--network", "none",
            "--memory", f"{LIMITS.memory_mb}m", "--memory-swap", f"{LIMITS.memory_mb}m",
            "--cpus", str(LIMITS.cpus), "--pids-limit", str(LIMITS.pids), "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--read-only", "--init",
            "--user", f"{os.getuid()}:{os.getgid()}", "--workdir", "/work",
            "--mount", f"type=bind,src={work},dst=/work",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=67108864,mode=1777",
            "--env", "HOME=/work/.sbx", "--env", "TMPDIR=/tmp",
            "--env", "PYTHONPATH=/work/.skills",
            "--env", "MPLBACKEND=Agg", "--env", "MPLCONFIGDIR=/work/.sbx",
        )
        if self.runtime:
            create.extend(["--runtime", self.runtime])
        create.extend([self.image, "/usr/bin/timeout", "-k", "2", str(timeout), *spec.argv])
        try:
            created = subprocess.run(create, capture_output=True, timeout=20)
        except (subprocess.TimeoutExpired, OSError) as error:
            raise RuntimeError("Docker could not create the sandbox container") from error
        if created.returncode != 0:
            detail = created.stderr.decode("utf-8", errors="replace")[:500]
            raise RuntimeError("Docker could not create the sandbox container: " + detail)

        def stop() -> None:
            try:
                subprocess.run(self._docker("kill", name), capture_output=True, timeout=8)
            except (subprocess.TimeoutExpired, OSError):
                pass

        try:
            return run_capped(self._docker("start", "--attach", name), timeout + 15, watch, stop)
        finally:
            try:
                subprocess.run(self._docker("rm", "--force", name), capture_output=True, timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                pass


class MicroVMRemoteBackend(Backend):
    """Sends the staged files to a separate runner (sandbox/runner_app.py) and takes the results back."""
    name = "remote"
    remote = True

    def __init__(self) -> None:
        self.url = os.getenv("SANDBOX_RUNNER_URL", "").rstrip("/")
        self.secret = os.getenv("SANDBOX_RUNNER_SECRET", "")

    @staticmethod
    def _cloudflare() -> tuple[str, str]:
        """Cloudflare Access service token (client id, secret), used when the runner is reached through a Cloudflare Tunnel."""
        return os.getenv("CF_ACCESS_CLIENT_ID", "").strip(), os.getenv("CF_ACCESS_CLIENT_SECRET", "").strip()

    def _headers(self) -> dict[str, str]:
        headers = {"X-Runner-Secret": self.secret}
        client_id, client_secret = self._cloudflare()
        if client_id and client_secret:
            headers["CF-Access-Client-Id"] = client_id
            headers["CF-Access-Client-Secret"] = client_secret
        return headers

    def _max_run_seconds(self) -> int | None:
        """Longest single run the runner is asked for. Cloudflare drops a proxied request that gets no response within 100 seconds,
        so behind a Tunnel runs are capped below that (SANDBOX_RUNNER_MAX_RUN_SECONDS overrides; on an Enterprise plan with a longer
        proxy timeout, raise it)."""
        raw = os.getenv("SANDBOX_RUNNER_MAX_RUN_SECONDS", "").strip()
        if raw:
            try:
                return max(5, min(int(raw), 3600))
            except ValueError:
                return None
        return 75 if all(self._cloudflare()) else None

    def status(self) -> tuple[bool, str]:
        client_id, client_secret = self._cloudflare()
        if bool(client_id) != bool(client_secret):
            return False, "Set both CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET for the Cloudflare Access service token, or neither."
        local_http = self.url.startswith("http://127.0.0.1") or self.url.startswith("http://localhost")
        if not self.url.startswith("https://") and not local_http and os.getenv("SANDBOX_RUNNER_ALLOW_HTTP") != "1":
            return False, "The sandbox runner URL must use HTTPS unless SANDBOX_RUNNER_ALLOW_HTTP=1 is set for a trusted private network."
        if len(self.secret) < 24:
            return False, "SANDBOX_RUNNER_SECRET must be at least 24 characters."
        import requests
        try:
            r = requests.get(f"{self.url}/health", headers=self._headers(), timeout=6)
            body = r.json() if r.ok else {}
        except Exception:  # noqa: BLE001
            return False, "The sandbox runner is not reachable."
        if not r.ok:
            if r.status_code in (401, 403):
                return False, "The runner refused the request. Check SANDBOX_RUNNER_SECRET and, behind Cloudflare, the Access service token and its policy."
            if r.status_code in (502, 504, 522, 523, 524, 530):
                return False, "The runner host could not be reached through the proxy. If you use a Cloudflare Tunnel, check that cloudflared and the runner are running."
            return False, "The sandbox runner is not reachable."
        if body.get("backend") != "microvm":
            return False, "The sandbox runner is not backed by a microVM."
        return (True, "remote microVM runner") if body.get("ok") else (False, body.get("detail") or "The microVM runner reports no working sandbox.")

    def run(self, spec: RunSpec, watch: Callable[[], str]) -> RawResult:
        import json

        import requests

        from sandbox.staging import tar_dir, untar_to

        payload = tar_dir(spec.work)
        cap = self._max_run_seconds()
        timeout = min(spec.timeout, cap) if cap else spec.timeout
        try:
            r = requests.post(f"{self.url}/run", headers=self._headers(), files={"work": ("work.tgz", payload, "application/gzip")},
                              data={"spec": json.dumps({"argv": spec.argv, "timeout": timeout, "session_key": spec.session_key})}, timeout=timeout + 60)
            r.raise_for_status()
            body = r.json()
        except (requests.RequestException, ValueError) as error:
            raise RuntimeError("The sandbox runner could not be reached.") from error
        for child in spec.work.iterdir():
            shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
        import base64

        untar_to(base64.b64decode(body.get("work", "")), spec.work, LIMITS.max_work_bytes * 2)
        meta = body.get("meta", {})
        stderr = str(meta.get("stderr", ""))[: LIMITS.output_cap]
        if meta.get("timed_out") and timeout < spec.timeout:
            stderr += f"\n[Stopped after {timeout}s: the runner is reached through a proxy that limits one request to about 100 seconds. Do the work in smaller steps.]"
        return RawResult(exit_code=int(meta.get("exit_code", -1)), stdout=str(meta.get("stdout", ""))[: LIMITS.output_cap], stderr=stderr,
                         timed_out=bool(meta.get("timed_out")), killed_reason=str(meta.get("killed_reason", "")))

    def destroy_session(self, session_key: str) -> None:
        import requests
        try:
            r = requests.post(f"{self.url}/destroy", headers=self._headers(), data={"session_key": session_key}, timeout=20)
            r.raise_for_status()
        except requests.RequestException:
            # Deleting a chat must never be blocked by an unavailable runner.
            return None


class GCPManagedRemoteBackend(MicroVMRemoteBackend):
    """Uses a pre-created GCE VM as the dedicated Firecracker runner host."""
    name = "gcp"

    def __init__(self) -> None:
        super().__init__()
        from sandbox.gcp_runner import GCPComputeRunner
        self.gcp = GCPComputeRunner()

    def _refresh_url(self) -> None:
        self.url = self.gcp.resolve_url().rstrip("/")

    def status(self) -> tuple[bool, str]:
        if len(self.secret) < 24:
            return False, "SANDBOX_RUNNER_SECRET must be at least 24 characters."
        try:
            self._refresh_url()
        except Exception as error:  # noqa: BLE001 - report a short actionable status, not provider internals
            return False, str(error) or "Could not resolve the GCP Firecracker runner."
        return super().status()

    def run(self, spec: RunSpec, watch: Callable[[], str]) -> RawResult:
        try:
            self._refresh_url()
        except Exception as error:  # noqa: BLE001
            raise RuntimeError("The GCP Firecracker runner is not available.") from error
        return super().run(spec, watch)

    def destroy_session(self, session_key: str) -> None:
        try:
            self._refresh_url()
        except Exception:  # noqa: BLE001 - session deletion remains best-effort
            return None
        return super().destroy_session(session_key)


class AWSManagedRemoteBackend(MicroVMRemoteBackend):
    """Uses a pre-created EC2 instance as the dedicated Firecracker runner host."""
    name = "aws"

    def __init__(self) -> None:
        super().__init__()
        from sandbox.aws_runner import AWSComputeRunner
        self.aws = AWSComputeRunner()

    def _refresh_url(self) -> None:
        self.url = self.aws.resolve_url().rstrip("/")

    def status(self) -> tuple[bool, str]:
        if len(self.secret) < 24:
            return False, "SANDBOX_RUNNER_SECRET must be at least 24 characters."
        try:
            self._refresh_url()
        except Exception as error:  # noqa: BLE001 - report actionable status, not provider internals
            return False, str(error) or "Could not resolve the AWS Firecracker runner."
        return super().status()

    def run(self, spec: RunSpec, watch: Callable[[], str]) -> RawResult:
        try:
            self._refresh_url()
        except Exception as error:  # noqa: BLE001
            raise RuntimeError("The AWS Firecracker runner is not available.") from error
        return super().run(spec, watch)

    def destroy_session(self, session_key: str) -> None:
        try:
            self._refresh_url()
        except Exception:  # noqa: BLE001 - session deletion remains best-effort
            return None
        return super().destroy_session(session_key)


_cache: dict = {"at": 0.0, "choice": "", "backend": None, "ok": False, "detail": ""}


def _factory_for(name: str):
    if name == "insecure-test":
        return InsecureTestBackend if os.getenv("APP_ENV", "").lower() not in ("production", "prod") else None
    return {"docker": DockerHostBackend, "microvm": MicroVMHostBackend, "gcp": GCPManagedRemoteBackend,
            "aws": AWSManagedRemoteBackend, "remote": MicroVMRemoteBackend, "auto": MicroVMRemoteBackend}.get(name)


def get_backend(force: bool = False) -> tuple[Backend, bool, str]:
    """Select a dedicated sandbox; fail closed otherwise.

    SANDBOX_BACKEND is one name (docker, microvm, gcp, aws, remote, auto) or, to opt in to failover, an ordered
    comma-separated list such as "aws,docker": the first healthy one is used. Unhealthy or unconfigured ones are
    skipped; if none is healthy, code execution is off (never direct host execution)."""
    choice = (os.getenv("SANDBOX_BACKEND", "auto") or "auto").strip().lower()
    if not force and choice == _cache.get("choice") and time.monotonic() - _cache["at"] < 60 and _cache["backend"] is not None:
        return _cache["backend"], _cache["ok"], _cache["detail"]
    names = list(dict.fromkeys(n.strip() for n in choice.split(",") if n.strip()))
    factories = [f for f in (_factory_for(n) for n in names) if f is not None]
    chosen: Backend = Backend()
    ok, detail = False, "Code execution is turned off: no supported sandbox is configured."
    reasons: list[str] = []
    for factory in factories:
        try:
            candidate = factory()
            usable, why = candidate.status()
        except Exception as error:  # noqa: BLE001 - a misconfigured backend must not break the others
            usable, why = False, str(error)[:200] or f"{factory.__name__} could not be started."
        if usable:
            chosen, ok, detail = candidate, True, why
            break
        if why and why not in reasons:
            reasons.append(why)
    if not ok and reasons:
        detail = " | ".join(reasons)
    _cache.update(at=time.monotonic(), choice=choice, backend=chosen, ok=ok, detail=detail)
    return chosen, ok, detail
