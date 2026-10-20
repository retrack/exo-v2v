#!/usr/bin/env bash
# Phase 0 receiver bootstrap (prototype for the future `ipxe` rescue profile).
#
# Runs once on a fresh Debian instance (from cloud-init). It copies the running
# system plus the agent into a RAM root filesystem, packs it as an initramfs and
# kexecs into it with the same kernel. After that, /dev/vda (and every data
# volume) is unused and can be overwritten by the sender.
#
# WARNING: once the sender has written to /dev/vda, any reboot boots the
# half-written disk. Recovery = recreate the receiver and redo the full copy.
#
# Env (set by make-user-data.sh): EXPECTED_DISKS (default 1), AGENT_DIR.
set -euxo pipefail
exec > >(tee -a /var/log/v2v-bootstrap.log) 2>&1

EXPECTED_DISKS="${EXPECTED_DISKS:-1}"
AGENT_DIR="${AGENT_DIR:-/opt/v2v-agent}"
R=/var/tmp/ramroot
IMG=/var/tmp/v2v-agent.img
export DEBIAN_FRONTEND=noninteractive

# 0. Started over SSH by `exo-v2v create` as soon as sshd answers: let cloud-init
#    finish first, and wait for apt/dpkg locks (unattended-upgrades on first boot).
command -v cloud-init >/dev/null && { cloud-init status --wait >/dev/null 2>&1 || true; }
APT="apt-get -o DPkg::Lock::Timeout=900"

# 1. Packages. virt-v2v brings libguestfs; virt-v2v-in-place must be present.
echo "kexec-tools kexec-tools/load_kexec boolean false" | debconf-set-selections
$APT update
$APT install -y --no-install-recommends kexec-tools zstd cpio rsync python3 \
  openssh-server qemu-utils virt-v2v libguestfs-tools xfsprogs xfsdump e2fsprogs gdisk
command -v virt-v2v-in-place || echo "WARN: virt-v2v-in-place missing; conversion step will fail (use the Fedora agent image)"

# 2. Wait for the data volumes the orchestrator attaches after creation.
#    Note: `ls` on a non-matching glob exits 2, which errexit+pipefail would
#    turn into a fatal error, so count with nullglob instead.
count_disks() { shopt -s nullglob; local d=(/sys/block/vd*); printf '%s' "${#d[@]}"; }
n=0
for _ in $(seq 1 90); do
  n="$(count_disks)"
  [ "$n" -ge "$EXPECTED_DISKS" ] && break
  sleep 10
done
ls -l /sys/block/
[ "$n" -ge "$EXPECTED_DISKS" ] || { echo "TIMEOUT: only $n of $EXPECTED_DISKS disks appeared"; exit 1; }

# 3. Build the RAM root from the running system.
#    /boot must stay: libguestfs/supermin looks there for a kernel to build its
#    appliance, and virt-v2v-in-place fails without one ("failed to find a
#    suitable kernel").
rm -rf "$R"; mkdir -p "$R"
rsync -aAXH --one-file-system \
  --exclude=/proc --exclude=/sys --exclude=/dev --exclude=/run --exclude=/tmp \
  --exclude=/var/tmp --exclude=/var/cache/apt/archives \
  --exclude=/var/lib/apt/lists --exclude=/var/log/journal --exclude=/swapfile \
  / "$R"/
mkdir -p "$R"/{proc,sys,dev,run,tmp,var/tmp,boot}
# /boot is a separate filesystem on some images (Ubuntu 24.04: ext4 vda16):
# --one-file-system skipped it, but libguestfs needs the kernel there.
if mountpoint -q /boot; then
  rsync -aAXH --exclude=/efi /boot/ "$R/boot/"
fi
ls "$R"/boot/vmlinuz-* >/dev/null 2>&1 || { echo "ERROR: no kernel in RAM-root /boot"; exit 1; }
chmod 1777 "$R/tmp" "$R/var/tmp"
: > "$R/etc/fstab"
ln -sf /lib/systemd/systemd "$R/init"
echo "v2v receiver ramroot built $(date -Is)" > "$R/etc/v2v-ramroot"

# Networking: DHCP on every NIC with systemd-networkd; keep current DNS.
mkdir -p "$R/etc/systemd/network"
cat > "$R/etc/systemd/network/10-v2v-dhcp.network" <<'EOF'
[Match]
Name=en* eth*
[Network]
DHCP=yes
EOF
rm -f "$R/etc/resolv.conf"; cp -L /etc/resolv.conf "$R/etc/resolv.conf"
chroot "$R" systemctl enable systemd-networkd ssh
chroot "$R" systemctl disable networking cloud-init cloud-init-local cloud-config cloud-final 2>/dev/null || true
# RAM root has no disk to protect: keep apt/unattended-upgrades from firing
# (re-downloads + installs into tmpfs) during the sync window.
chroot "$R" systemctl disable apt-daily.timer apt-daily-upgrade.timer apt-listchanges.timer \
  unattended-upgrades fstrim.timer e2scrub_all.timer dpkg-db-backup.timer man-db.timer 2>/dev/null || true
mkdir -p "$R/etc/cloud"; touch "$R/etc/cloud/cloud-init.disabled"

# Agent service.
cat > "$R/etc/systemd/system/v2v-agent.service" <<EOF
[Unit]
Description=v2v receiver agent (NBD exports + control API)
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=/usr/bin/python3 -I $AGENT_DIR/agent.py
Restart=on-failure
[Install]
WantedBy=multi-user.target
EOF
chroot "$R" systemctl enable v2v-agent

# 4. Pack and kexec with the same kernel and command line (minus root=).
( cd "$R" && find . -xdev -print0 | cpio --null -o -H newc --quiet | zstd -T0 -3 ) > "$IMG"
ls -lh "$IMG"; free -m
KVER="$(uname -r)"
# Strip boot/root tokens token-exactly (a prefix regex would eat e.g. rootwait).
CMDLINE="$(tr ' ' '\n' < /proc/cmdline | grep -vE '^(BOOT_IMAGE|root|ro|rw|rootflags)(=.*)?$' | tr '\n' ' ')rdinit=/init"
# Make room for a 1-2 GB initrd: the old kernel's page cache can fragment memory.
sync; echo 3 > /proc/sys/vm/drop_caches
kexec -s -l "/boot/vmlinuz-$KVER" --initrd="$IMG" --command-line="$CMDLINE" \
  || kexec -l "/boot/vmlinuz-$KVER" --initrd="$IMG" --command-line="$CMDLINE"
# Fail loudly instead of a silent fallback to a normal reboot into a half-built disk.
[ "$(cat /sys/kernel/kexec_loaded)" = 1 ] || { echo "ERROR: kexec image not loaded"; exit 1; }
systemctl kexec
