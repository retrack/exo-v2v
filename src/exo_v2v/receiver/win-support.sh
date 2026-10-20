#!/usr/bin/env bash
# win-support.sh — add Windows conversion support to a Phase 0 (Debian RAM-root)
# receiver. Run as root on the receiver BEFORE POST /convert of a Windows guest:
#   ssh debian@<receiver> sudo bash -s < tools/receiver/win-support.sh
# Debian packages neither piece virt-v2v needs for Windows:
#   - virtio-win.iso (viostor/vioscsi/netkvm drivers) → /usr/share/virtio-win/
#     (agent.py exports VIRTIO_WIN when this file exists)
#   - rhsrvany.exe + pnp_wait.exe (firstboot service) → /usr/share/virt-tools/
#     (taken from Fedora's noarch mingw32-srvany RPM)
# Both land in the RAM root (~0.85 GB of RAM; fine on standard.large).
set -euo pipefail
VIRTIO_URL="${VIRTIO_URL:-https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/archive-virtio/virtio-win-0.1.302-1/virtio-win.iso}"
SRVANY_URL="${SRVANY_URL:-https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Everything/x86_64/os/Packages/m/mingw32-srvany-1.1-12.fc44.noarch.rpm}"

command -v cpio >/dev/null && command -v zstd >/dev/null || apt-get install -y --no-install-recommends cpio zstd xz-utils curl >/dev/null
tmp=$(mktemp -d)
curl -fsSL -o "$tmp/srvany.rpm" "$SRVANY_URL"
# RPM = lead (96 B) + signature header (8-aligned) + header + compressed cpio.
# Parsed here: rpm2cpio is not packaged on Ubuntu 24.04.
python3 - "$tmp/srvany.rpm" "$tmp/payload" <<'PY'
import struct, sys
b = open(sys.argv[1], "rb").read(); o = 96
for i in range(2):
    assert b[o:o + 3] == b"\x8e\xad\xe8", "not an RPM header"
    n, s = struct.unpack(">II", b[o + 8:o + 16]); o += 16 + 16 * n + s
    if i == 0:
        o = (o + 7) & ~7
open(sys.argv[2], "wb").write(b[o:])
PY
( cd "$tmp" && { zstd -dc payload 2>/dev/null || xz -dc payload; } | cpio -idm --quiet )
install -D -m 644 "$tmp"/usr/i686-w64-mingw32/sys-root/mingw/bin/rhsrvany.exe /usr/share/virt-tools/rhsrvany.exe
install -D -m 644 "$tmp"/usr/i686-w64-mingw32/sys-root/mingw/bin/pnp_wait.exe  /usr/share/virt-tools/pnp_wait.exe
rm -rf "$tmp"
mkdir -p /usr/share/virtio-win
curl -fsSL -o /usr/share/virtio-win/virtio-win.iso.part "$VIRTIO_URL"
mv /usr/share/virtio-win/virtio-win.iso.part /usr/share/virtio-win/virtio-win.iso
ls -la /usr/share/virt-tools /usr/share/virtio-win
free -m | head -2
echo WIN_SUPPORT_OK
