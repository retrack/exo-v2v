#!/usr/bin/env bash
# build-image.sh — build the exo-v2v receiver template image (qcow2).
#
#   sudo template/build-image.sh linux|windows OUTPUT.qcow2
#
# Run as root on Ubuntu 24.04 (needs mmdebstrap, qemu-utils, gdisk, dosfstools,
# grub-pc-bin, grub-efi-amd64-signed, shim-signed). Network access to the
# Ubuntu archive (and, for `windows`, to fedorapeople.org / fedoraproject.org).
#
# The image boots, in BIOS (legacy) or UEFI mode, with or without Secure Boot
# (Canonical-signed shim -> grub -> kernel), straight into a receiver that runs
# entirely from RAM: the kernel's initramfs IS the root filesystem. The disk is
# never mounted, so `exo-v2v sync` can overwrite it with the migrated VM.
#
# At boot the receiver reads the migration's SSH key from the instance
# user-data (`ssh_authorized_keys` in a #cloud-config), for user `v2v`.
# `exo-v2v create` then streams the agent code over SSH and starts it: the
# image carries the tools (virt-v2v, libguestfs, ...), not exo-v2v itself.
#
# Flavours: `linux`; `windows` = linux + virtio-win drivers and rhsrvany
# (what virt-v2v needs to convert Windows guests), so a Windows migration
# needs no internet access from the receiver.
set -euo pipefail
FLAVOUR="${1:?flavour: linux|windows}"; OUT="${2:?output .qcow2}"
case "$FLAVOUR" in linux|windows) ;; *) echo "flavour must be linux or windows" >&2; exit 2;; esac
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
SUITE="${SUITE:-noble}"
MIRROR="${MIRROR:-http://archive.ubuntu.com/ubuntu}"
SECURITY="${SECURITY:-http://security.ubuntu.com/ubuntu}"
VERSION="${VERSION:-$(date -u +%Y%m%d)}"
DISK_GB="${DISK_GB:-10}"            # Exoscale's minimum disk size
ESP_MB="${ESP_MB:-4096}"            # holds the kernel and the RAM-root initramfs
W="$(mktemp -d /var/tmp/v2v-template.XXXXXX)"
R="$W/root"
cleanup() {
  set +e
  mountpoint -q "$W/esp" && umount "$W/esp"
  mountpoint -q "$W/root/proc" && umount "$W/root/proc"
  [ -n "${LOOP:-}" ] && losetup -d "$LOOP"
  rm -rf "$W"
}
trap cleanup EXIT

PKGS=(
  linux-image-virtual systemd systemd-sysv systemd-resolved udev kmod dbus
  openssh-server sudo iproute2 curl ca-certificates python3
  virt-v2v libguestfs-tools libguestfs-xfs qemu-utils qemu-system-x86
  xfsprogs xfsdump e2fsprogs ntfs-3g dosfstools gdisk rsync cpio zstd xz-utils
)

echo "== 1. root filesystem ($SUITE)"
mmdebstrap --variant=minbase --components=main,universe \
  --include="$(IFS=,; echo "${PKGS[*]}")" \
  --customize-hook='rm -f "$1"/etc/ssh/ssh_host_*' \
  "$SUITE" "$R" \
  "deb $MIRROR $SUITE main universe" "deb $MIRROR $SUITE-updates main universe" \
  "deb $SECURITY $SUITE-security main universe"
command -v "$R/usr/bin/virt-v2v-in-place" >/dev/null 2>&1 || ls "$R"/usr/bin/virt-v2v-in-place

if [ "$FLAVOUR" = windows ]; then
  echo "== 1b. Windows conversion support (virtio-win, rhsrvany)"
  rm -f "$R/etc/resolv.conf"; cp -L /etc/resolv.conf "$R/etc/resolv.conf"
  mount -t proc proc "$R/proc"
  chroot "$R" bash -s < "$HERE/../src/exo_v2v/receiver/win-support.sh" | tail -2
  umount "$R/proc"
  # Keep them out of the initramfs: GRUB loads the initramfs through firmware
  # disk I/O, and a 400+ MB one sometimes never finished loading on Exoscale.
  # They stay on the EFI partition; v2v-winsupport copies them into RAM at
  # boot (Linux virtio I/O), before SSH - so before anything can overwrite the disk.
  mkdir -p "$W/esp-content/v2v/win"
  mv "$R/usr/share/virtio-win/virtio-win.iso" "$W/esp-content/v2v/win/"
  mv "$R/usr/share/virt-tools/rhsrvany.exe" "$R/usr/share/virt-tools/pnp_wait.exe" "$W/esp-content/v2v/win/"
  cat > "$R/etc/systemd/system/v2v-winsupport.service" <<'EOF2'
[Unit]
Description=Copy virtio-win and rhsrvany from the template's EFI partition into RAM
After=systemd-udevd.service
Before=ssh.service v2v-agent.service
[Service]
Type=oneshot
ExecStart=/bin/sh -ec 'for i in $(seq 1 120); do blkid -L V2VESP >/dev/null && break; sleep 1; done; \
  m=$(mktemp -d); mount -o ro LABEL=V2VESP "$m"; \
  install -D -m 644 "$m/v2v/win/virtio-win.iso" /usr/share/virtio-win/virtio-win.iso; \
  install -D -m 644 "$m/v2v/win/rhsrvany.exe" /usr/share/virt-tools/rhsrvany.exe; \
  install -D -m 644 "$m/v2v/win/pnp_wait.exe" /usr/share/virt-tools/pnp_wait.exe; umount "$m"'
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF2
  chroot "$R" systemctl enable v2v-winsupport
fi

echo "== 2. receiver configuration"
echo "exo-v2v receiver template $FLAVOUR $VERSION" > "$R/etc/v2v-ramroot"
echo "$FLAVOUR $VERSION" > "$R/etc/v2v-template"
echo "v2v-receiver" > "$R/etc/hostname"
: > "$R/etc/fstab"
ln -sf /lib/systemd/systemd "$R/init"
mkdir -p "$R/etc/systemd/network"
cat > "$R/etc/systemd/network/10-v2v-dhcp.network" <<'EOF'
[Match]
Name=en* eth*
[Network]
DHCP=yes
EOF
# user v2v: SSH key only, passwordless sudo
chroot "$R" useradd -m -s /bin/bash v2v
echo "v2v ALL=(ALL) NOPASSWD:ALL" > "$R/etc/sudoers.d/v2v"; chmod 440 "$R/etc/sudoers.d/v2v"
install -m 755 "$HERE/v2v-userdata-key" "$R/usr/local/sbin/v2v-userdata-key"
cat > "$R/etc/systemd/system/v2v-hostkeys.service" <<'EOF'
[Unit]
Description=Generate SSH host keys (each receiver gets its own)
Before=ssh.service ssh.socket
[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
cat > "$R/etc/systemd/system/v2v-userdata-key.service" <<'EOF'
[Unit]
Description=Install the migration's SSH key from the instance user-data
After=network-online.target
Wants=network-online.target
Before=ssh.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/v2v-userdata-key v2v
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
# The agent itself is streamed by `exo-v2v create` to /opt/v2v-agent.
cat > "$R/etc/systemd/system/v2v-agent.service" <<'EOF'
[Unit]
Description=v2v receiver agent (NBD exports + control API)
After=network-online.target
Wants=network-online.target
ConditionPathExists=/opt/v2v-agent/agent.py
[Service]
ExecStart=/usr/bin/python3 -I /opt/v2v-agent/agent.py
Restart=on-failure
[Install]
WantedBy=multi-user.target
EOF
rm -f "$R/etc/resolv.conf"; ln -s /run/systemd/resolve/stub-resolv.conf "$R/etc/resolv.conf"
chroot "$R" systemctl disable ssh.socket 2>/dev/null || true
chroot "$R" systemctl enable systemd-networkd systemd-resolved ssh v2v-hostkeys v2v-userdata-key v2v-agent
chroot "$R" systemctl mask apt-daily.timer apt-daily-upgrade.timer fstrim.timer e2scrub_all.timer \
  dpkg-db-backup.timer man-db.timer motd-news.timer 2>/dev/null || true
chroot "$R" systemctl set-default multi-user.target
rm -f "$R"/boot/initrd.img*            # the RAM root needs none; supermin needs only the kernel
rm -rf "$R"/var/lib/apt/lists/* "$R"/var/cache/apt/*.bin "$R"/var/cache/apt/archives/*.deb

echo "== 3. RAM-root initramfs"
KVER="$(ls "$R"/lib/modules | sort -V | tail -1)"
mkdir -p "$W/esp-content/v2v"
cp "$R/boot/vmlinuz-$KVER" "$W/esp-content/v2v/vmlinuz"       # Canonical-signed
( cd "$R" && find . -xdev -print0 | cpio --null -o -H newc --quiet | zstd -T0 -10 ) \
  > "$W/esp-content/v2v/initrd.zst"
ls -lh "$W/esp-content/v2v/"
SIZE=$(du -sm "$W/esp-content" | cut -f1)
[ "$SIZE" -lt $((ESP_MB - 64)) ] || { echo "ESP too small for $SIZE MiB" >&2; exit 1; }

echo "== 4. disk: GPT, BIOS boot partition + EFI system partition"
RAW="$W/disk.raw"
truncate -s "${DISK_GB}G" "$RAW"
sgdisk -Z -n1:2048:+1M -t1:EF02 -c1:biosboot -n2:0:+"${ESP_MB}M" -t2:EF00 -c2:v2v-esp "$RAW" >/dev/null
LOOP=$(losetup -fP --show "$RAW")
mkfs.vfat -F32 -n V2VESP "${LOOP}p2" >/dev/null
mkdir -p "$W/esp"; mount "${LOOP}p2" "$W/esp"
cp -r "$W/esp-content/v2v" "$W/esp/"
# UEFI: shim (Microsoft-signed) -> grub (Canonical-signed) from the fallback path
mkdir -p "$W/esp/EFI/BOOT" "$W/esp/EFI/ubuntu"
cp /usr/lib/shim/shimx64.efi.signed "$W/esp/EFI/BOOT/BOOTX64.EFI"
cp /usr/lib/shim/mmx64.efi "$W/esp/EFI/BOOT/"
cp /usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed "$W/esp/EFI/BOOT/grubx64.efi"
# BIOS: grub i386-pc in the MBR + biosboot partition, config on the ESP
grub-install --target=i386-pc --boot-directory="$W/esp/boot" --modules="part_gpt fat" "$LOOP" 2>&1 | tail -1
CMDLINE="rdinit=/init console=tty0 console=ttyS0,115200n8 net.ifnames=1 systemd.show_status=1"
GRUBCFG="serial --unit=0 --speed=115200
terminal_input console serial
terminal_output console serial
set timeout=1
set default=0
search --no-floppy --label V2VESP --set root
menuentry 'exo-v2v receiver ($FLAVOUR $VERSION)' {
  linux /v2v/vmlinuz $CMDLINE
  initrd /v2v/initrd.zst
}"
# signed grub looks for its config next to itself or in /EFI/ubuntu; i386-pc in /boot/grub
for d in EFI/BOOT EFI/ubuntu boot/grub; do mkdir -p "$W/esp/$d"; echo "$GRUBCFG" > "$W/esp/$d/grub.cfg"; done
sync; umount "$W/esp"; losetup -d "$LOOP"; LOOP=

echo "== 5. qcow2"
qemu-img convert -c -O qcow2 "$RAW" "$OUT"
md5sum "$OUT" | tee "$OUT.md5"
ls -lh "$OUT"
echo "BUILD_OK $FLAVOUR $VERSION kernel $KVER"
