#!/usr/bin/env bash
set -euo pipefail

# Install the trusted guest agent into a dedicated ext4 rootfs.
# Run only during image provisioning, never from the web application.
ROOTFS=${1:?usage: install_guest_agent.sh /path/to/agent-rootfs.ext4}
SOURCE_DIR=${2:-$(cd "$(dirname "$0")" && pwd)}
MOUNT_DIR=$(mktemp -d)
cleanup() { mountpoint -q "$MOUNT_DIR" && umount "$MOUNT_DIR" || true; rm -rf "$MOUNT_DIR"; }
trap cleanup EXIT

[[ -f "$ROOTFS" ]] || { echo "rootfs not found" >&2; exit 1; }
mount -o loop,rw "$ROOTFS" "$MOUNT_DIR"
install -D -m 0755 "$SOURCE_DIR/guest_agent.py" "$MOUNT_DIR/usr/local/libexec/agent-guest-agent.py"
install -D -m 0644 "$SOURCE_DIR/agent-guest.service" "$MOUNT_DIR/etc/systemd/system/agent-guest.service"
mkdir -p "$MOUNT_DIR/etc/systemd/system/multi-user.target.wants"
ln -sfn ../agent-guest.service "$MOUNT_DIR/etc/systemd/system/multi-user.target.wants/agent-guest.service"
mkdir -p "$MOUNT_DIR/work"
chmod 0700 "$MOUNT_DIR/work"

echo "guest agent installed into $ROOTFS"
