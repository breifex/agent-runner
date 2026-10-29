#!/usr/bin/env bash
set -euo pipefail

# Build the guest root filesystem (ext4) the microVMs boot from, and install the guest agent into it.
# Run as root on the runner host, with internet access (only for the build; the VMs themselves have no network).
#   sudo ./build_rootfs.sh [/var/lib/agent-microvm/agent-rootfs.ext4]
# Environment: UBUNTU_SUITE (default noble), ROOTFS_SIZE_MB (default 6144), WITH_OFFICE=1 adds LibreOffice (large), UBUNTU_MIRROR.
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
OUT=${1:-/var/lib/agent-microvm/agent-rootfs.ext4}
SUITE=${UBUNTU_SUITE:-noble}
SIZE_MB=${ROOTFS_SIZE_MB:-6144}
MIRROR=${UBUNTU_MIRROR:-http://archive.ubuntu.com/ubuntu}
SOURCE_DIR=$(cd "$(dirname "$0")" && pwd)
command -v debootstrap >/dev/null || { echo "install debootstrap first: apt-get install -y debootstrap e2fsprogs" >&2; exit 1; }

WORK=$(mktemp -d /var/tmp/rootfs-build.XXXXXX)
ROOT="$WORK/root"
cleanup() {
  for m in dev/pts dev proc sys; do mountpoint -q "$ROOT/$m" && umount -l "$ROOT/$m" || true; done
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "== 1/5 base system ($SUITE)"
debootstrap --variant=minbase \
  --include=systemd,systemd-sysv,python3,python3-pip,bash,coreutils,tar,gzip,ca-certificates \
  "$SUITE" "$ROOT" "$MIRROR"

echo "== 2/5 tools and Python libraries (same set as the Docker sandbox image)"
mount -t proc proc "$ROOT/proc"; mount -t sysfs sys "$ROOT/sys"; mount --bind /dev "$ROOT/dev"; mount -t devpts devpts "$ROOT/dev/pts"
cp /etc/resolv.conf "$ROOT/etc/resolv.conf"
APT_EXTRA="pandoc imagemagick ghostscript poppler-utils tesseract-ocr graphviz fonts-dejavu fonts-liberation libmagic1 zip unzip jq"
[[ "${WITH_OFFICE:-0}" == "1" ]] && APT_EXTRA="$APT_EXTRA libreoffice-writer libreoffice-calc libreoffice-impress"
chroot "$ROOT" /bin/bash -euo pipefail -c "
  export DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1
  apt-get update
  apt-get install -y --no-install-recommends $APT_EXTRA
  pip install --break-system-packages \
    pandas numpy scipy matplotlib seaborn openpyxl xlsxwriter python-docx python-pptx reportlab pypdf pdfplumber pillow beautifulsoup4 lxml \
    markdown pyyaml jinja2 sympy scikit-learn pytesseract python-dateutil tabulate
  apt-get clean
  rm -rf /var/lib/apt/lists/* /root/.cache /tmp/*
"

echo "== 3/5 boot settings"
echo agent-guest > "$ROOT/etc/hostname"
mkdir -p "$ROOT/work" "$ROOT/etc/systemd/system"
chmod 0700 "$ROOT/work"
# No network, no logins, no timers: the VM only has to boot fast and run the guest agent.
for unit in systemd-networkd.service systemd-networkd-wait-online.service systemd-resolved.service systemd-timesyncd.service \
            apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service e2scrub_all.timer fstrim.timer \
            motd-news.timer getty@.service serial-getty@ttyS0.service console-getty.service; do
  ln -sf /dev/null "$ROOT/etc/systemd/system/$unit"
done
: > "$ROOT/etc/resolv.conf"
umount -l "$ROOT/dev/pts" "$ROOT/dev" "$ROOT/proc" "$ROOT/sys" 2>/dev/null || true

echo "== 4/5 ext4 image (${SIZE_MB} MB)"
mkdir -p "$(dirname "$OUT")"
truncate -s "${SIZE_MB}M" "$WORK/rootfs.ext4"
mkfs.ext4 -q -F -L agent-rootfs -d "$ROOT" "$WORK/rootfs.ext4"

echo "== 5/5 guest agent"
"$SOURCE_DIR/install_guest_agent.sh" "$WORK/rootfs.ext4" "$SOURCE_DIR"
install -m 0644 -o root -g root "$WORK/rootfs.ext4" "$OUT"
echo "rootfs ready: $OUT"
