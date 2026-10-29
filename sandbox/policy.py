"""Rules for running code: limits, the static scanner, and the command filter.

The sandbox (see backends.py) is what actually keeps users apart. This module is the second layer: it refuses obviously hostile
code early, records attempts as strikes, and gives admins something to review. Static scanning is best-effort and can be
bypassed, which is exactly why it is never the only protection."""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Limits:
    default_timeout: int = 60
    max_timeout: int = 300
    memory_mb: int = _int("SANDBOX_MEMORY_MB", 1024)
    cpus: float = float(os.getenv("SANDBOX_CPUS", "") or 1)
    pids: int = _int("SANDBOX_PIDS", 128)
    max_file_bytes: int = _int("SANDBOX_MAX_FILE_MB", 50) * 1024 * 1024
    max_work_bytes: int = _int("SANDBOX_MAX_WORK_MB", 200) * 1024 * 1024
    max_files: int = _int("SANDBOX_MAX_FILES", 2000)
    output_cap: int = 100_000
    runs_per_hour: int = _int("SANDBOX_RUNS_PER_HOUR", 60)
    global_concurrency: int = _int("SANDBOX_MAX_CONCURRENT", 4)
    strike_limit: int = _int("SANDBOX_STRIKE_LIMIT", 5)       # strikes within 24h before a user is suspended from running code
    max_code_chars: int = 20_000


LIMITS = Limits()


@dataclass
class Finding:
    severity: str      # "block" | "warn"
    message: str
    line: int = 0

    def as_dict(self) -> dict:
        return {"severity": self.severity, "message": self.message, "line": self.line}


# ---- things that only make sense when attacking the host or other users
_BLOCK_IMPORTS = {"ctypes": "native code can be used to attack the sandbox", "cffi": "native code can be used to attack the sandbox",
                  "pty": "opens terminals", "pwd": "reads system accounts", "grp": "reads system groups", "crypt": "system password hashing"}
_BLOCK_CALLS = {"os.setuid", "os.seteuid", "os.setgid", "os.chroot", "os.mount", "os.ptrace", "os.fork", "os.forkpty", "os.setns", "os.unshare",
                "os.kill", "os.killpg", "os.setsid", "os.mknod", "os.mkfifo"}
_WARN_IMPORTS = {"socket": "no network is available in the sandbox", "subprocess": "starts other programs", "requests": "no network is available",
                 "urllib": "no network is available", "http": "no network is available", "multiprocessing": "starts many processes",
                 "threading": "starts threads", "signal": "signal handling"}
_WARN_CALLS = {"eval", "exec", "compile", "__import__", "os.system", "os.popen", "os.environ", "os.getenv", "getpass.getuser"}
_BLOCK_STRINGS = [
    (re.compile(r"/proc/(self|\d+|\*)/(environ|mem|maps|root|cwd|fd)"), "reads process internals"),
    (re.compile(r"docker\.sock|/var/run/docker|containerd"), "targets the container runtime"),
    (re.compile(r"169\.254\.169\.254|metadata\.google\.internal|100\.100\.100\.200"), "targets the cloud metadata service"),
    (re.compile(r"/etc/(shadow|sudoers|ssh)|\.ssh/|id_rsa|\.aws/credentials|\.env\b"), "looks for credentials"),
    (re.compile(r"/sys/fs/cgroup|/sys/kernel|/dev/(mem|kmem|sd[a-z]|nvme)"), "targets host devices or cgroups"),
    (re.compile(r"\bnsenter\b|\bunshare\b|\bchroot\b|\bpivot_root\b|\binsmod\b|\bmodprobe\b"), "tries to leave or alter the sandbox"),
    (re.compile(r"\.\./\.\./|/workspace/\d+/|/home/[a-z_][a-z0-9_-]*/"), "reaches for other folders"),
]
_WARN_STRINGS = [(re.compile(r"/etc/passwd"), "reads the system account list"), (re.compile(r"\bhttps?://"), "mentions a web address (no network in the sandbox)")]

_SHELL_ALLOWED = {"awk", "cat", "convert", "cp", "cut", "date", "diff", "dot", "echo", "file", "find", "grep", "head", "identify", "jq", "ls", "mkdir", "pandoc", "pdftoppm", "pdftotext", "python", "python3", "sed", "soffice", "sort", "tail", "tesseract", "tr", "unzip", "wc", "zip"}
_SHELL_META = re.compile(r"(?:[;&|`$<>]|\$\(|\)|\n\s*(?:rm|mv|cp|chmod|chown|kill)\b)")

_SHELL_BLOCK = [
    (re.compile(r"/dev/(tcp|udp)/"), "opens network sockets from the shell"),
    (re.compile(r"\b(nc|ncat|netcat|socat)\b.*\s-[a-z]*e\b"), "reverse-shell pattern"),
    (re.compile(r"\b(sudo|su|doas|chmod\s+[0-7]*[4-7][0-7]{3}|chmod\s+\+s|setcap|capsh|mount|umount|losetup)\b"), "privilege or mount commands"),
    (re.compile(r":\s*\(\s*\)\s*\{.*\};\s*:"), "fork bomb"),
    (re.compile(r"\bdocker\b|\bpodman\b|\bkubectl\b|\bcrictl\b|\bsystemctl\b|\bservice\s"), "container or service control"),
    (re.compile(r"\bdd\s+if=/dev/(zero|urandom|random)\b.*of=(?!/work)"), "disk-filling pattern"),
]


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def scan_python(code: str) -> list[Finding]:
    findings: list[Finding] = []
    if len(code) > LIMITS.max_code_chars:
        return [Finding("block", f"The code is longer than {LIMITS.max_code_chars:,} characters.")]
    try:
        tree = ast.parse(code)
    except SyntaxError as error:
        return [Finding("block", f"The code has a syntax error: {error.msg}", error.lineno or 0)]
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for m in mods:
                root = m.split(".")[0]
                if root in _BLOCK_IMPORTS:
                    findings.append(Finding("block", f"import {root}: {_BLOCK_IMPORTS[root]}", line))
                elif root in _WARN_IMPORTS:
                    findings.append(Finding("warn", f"import {root}: {_WARN_IMPORTS[root]}", line))
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in _BLOCK_CALLS:
                findings.append(Finding("block", f"{name}() is not allowed", line))
            elif name in _WARN_CALLS:
                findings.append(Finding("warn", f"{name}() runs dynamic or system code", line))
        elif isinstance(node, ast.Attribute) and _dotted(node) in _WARN_CALLS:
            findings.append(Finding("warn", f"{_dotted(node)} reads the environment", line))
    for i, text in enumerate(code.splitlines(), 1):
        for rx, why in _BLOCK_STRINGS:
            if rx.search(text):
                findings.append(Finding("block", why, i))
        for rx, why in _WARN_STRINGS:
            if rx.search(text):
                findings.append(Finding("warn", why, i))
    seen, unique = set(), []
    for f in findings:
        key = (f.severity, f.message, f.line)
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique[:50]


def scan_shell(command: str) -> list[Finding]:
    findings = [Finding("block", why) for rx, why in _SHELL_BLOCK + [(r, w) for r, w in _BLOCK_STRINGS] if rx.search(command)]
    if len(command) > 8000:
        findings.append(Finding("block", "The command is too long."))
    if _SHELL_META.search(command):
        findings.append(Finding("block", "shell operators and command chaining are not allowed."))
    for token in re.findall(r"(?:^|\s)([A-Za-z0-9_.+-]+)(?=\s|$)", command):
        if token not in _SHELL_ALLOWED and not token.startswith("/work/"):
            findings.append(Finding("block", f"the command '{token}' is not on the sandbox allowlist."))
    return findings


def blocked(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == "block"]
