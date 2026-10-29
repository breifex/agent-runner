#!/usr/bin/env python3
"""Destroy Firecracker session VMs that were abandoned without API deletion."""
from __future__ import annotations
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(os.getenv("FIRECRACKER_RUN_ROOT", "/var/lib/agent-microvm/sessions"))
EXECUTOR = os.getenv("SANDBOX_MICROVM_EXECUTOR", "/usr/local/bin/agent-microvm-exec")
TTL = int(os.getenv("FIRECRACKER_SESSION_TTL_SECONDS", str(24 * 60 * 60)))


def main() -> int:
    now = time.time()
    if not ROOT.is_dir():
        return 0
    for state in ROOT.iterdir():
        if not state.is_dir():
            continue
        marker = state / "last_used"
        key_file = state / "session.key"
        if not marker.exists() or not key_file.is_file():
            continue
        if now - marker.stat().st_mtime <= TTL:
            continue
        key = key_file.read_text(encoding="utf-8").strip()
        subprocess.run([EXECUTOR, "--destroy", "--session-key", key], check=False,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env={"PATH": "/usr/bin:/bin:/usr/local/bin", **{
                           name: value for name, value in os.environ.items()
                           if name.startswith("FIRECRACKER_") or name == "SANDBOX_MICROVM_EXECUTOR"
                       }})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
