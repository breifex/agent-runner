#!/usr/bin/env bash
set -euo pipefail

# Provision a dedicated Linux runner host. This script does not download a
# hypervisor or guest image; pin and verify those artifacts through your normal
# release process before running it.
APP_DIR=${APP_DIR:-/opt/agent-microvm}
RUN_ROOT=${RUN_ROOT:-/var/lib/agent-microvm/sessions}
BIN_DIR=${BIN_DIR:-/usr/local/bin}
SERVICE_USER=${SERVICE_USER:-agentvm}
SOURCE_DIR=${1:?usage: install_runner.sh /path/to/backend/sandbox}

id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -o root -g root -m 0755 "$APP_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$RUN_ROOT"
install -o root -g root -m 0755 "$SOURCE_DIR/firecracker_executor.py" "$APP_DIR/firecracker_executor.py"
install -o root -g root -m 0755 "$SOURCE_DIR/reap_sessions.py" "$APP_DIR/reap_sessions.py"
install -o root -g root -m 0755 "$SOURCE_DIR/agent-microvm-exec" "$BIN_DIR/agent-microvm-exec"
install -o root -g root -m 0644 "$SOURCE_DIR/agent-microvm-reaper.service" /etc/systemd/system/agent-microvm-reaper.service
install -o root -g root -m 0644 "$SOURCE_DIR/agent-microvm-reaper.timer" /etc/systemd/system/agent-microvm-reaper.timer

# The runner API and the executor run as root because the jailer needs root; Firecracker itself then runs as FIRECRACKER_UID (default nobody).
# SERVICE_USER only owns the session directory and is kept for the optional FIRECRACKER_USE_JAILER=0 mode.
getent group kvm >/dev/null && usermod -a -G kvm "$SERVICE_USER" || true

# The runner API needs only these two packages from the backend (sandbox and workspace_manager).
install -d -o root -g root -m 0755 "$APP_DIR/app"
rm -rf "$APP_DIR/app/sandbox" "$APP_DIR/app/workspace_manager"
cp -a "$SOURCE_DIR" "$APP_DIR/app/sandbox"
cp -a "$SOURCE_DIR/../workspace_manager" "$APP_DIR/app/workspace_manager"
find "$APP_DIR/app" -name __pycache__ -type d -prune -exec rm -rf {} +
install -o root -g root -m 0644 "$SOURCE_DIR/agent-runner.service" /etc/systemd/system/agent-runner.service
chown -R root:root "$APP_DIR"
chmod 0755 "$APP_DIR/firecracker_executor.py"

test -x "${FIRECRACKER_BIN:-/usr/local/bin/firecracker}"
test -x "${FIRECRACKER_JAILER:-/usr/local/bin/jailer}"
test -r "${FIRECRACKER_KERNEL:-/var/lib/agent-microvm/vmlinux}"
test -r "${FIRECRACKER_ROOTFS:-/var/lib/agent-microvm/agent-rootfs.ext4}"
test -r /dev/kvm && test -w /dev/kvm
systemctl daemon-reload
systemctl enable --now agent-microvm-reaper.timer

echo "runner installed. Next: create the venv, write /etc/agent-runner.env, then: systemctl enable --now agent-runner (see RUNNER_SETUP.md)"
