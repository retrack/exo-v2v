#!/usr/bin/env bash
# test_guestfix.sh — local test of guestfix.py 4Kn adaptation (needs sudo, loop devices).
# Builds disks the way a VMware guest would have them (512-byte sectors), attaches
# them as 4096-byte-sector loop devices (= Exoscale Block Storage) and checks:
#   GPT: partitions invisible before adapt, visible + mountable after (ext4 4k);
#        XFS sectsz=512 partition reported incompatible.
#   MBR: same with an msdos table.
#   whole-disk ext4 (no table): compatible, no action.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/../src/exo_v2v/receiver" && pwd)"
W="$(mktemp -d)"; trap 'sudo umount "$W"/m 2>/dev/null; for l in $LOOPS; do sudo losetup -d $l; done; rm -rf "$W"' EXIT
LOOPS=""; fail=0
ok() { echo "  ok: $*"; }; bad() { echo "  FAIL: $*"; fail=1; }
mkdir -p "$W/m"

mk512() {  # $1=img $2=table(gpt|msdos|none)
  truncate -s 2G "$1"
  local l; l=$(sudo losetup -f --show --sector-size 512 "$1")
  if [ "$2" != none ]; then
    sudo parted -s "$l" mklabel "$2" mkpart primary 1MiB 1025MiB mkpart primary 1025MiB 100%
    sudo partprobe "$l"; sleep 1
    sudo mkfs.ext4 -q -b 4096 -L v2vext "${l}p1"
    sudo mkfs.xfs -q -f -s size=512 -L v2vxfs "${l}p2"
    sudo mount "${l}p1" "$W/m"; echo marker | sudo tee "$W/m/marker" >/dev/null; sudo umount "$W/m"
  else
    sudo mkfs.ext4 -q -b 4096 -L whole "$l"
  fi
  sudo losetup -d "$l"
}

for t in gpt msdos none; do
  echo "== $t"
  img="$W/$t.img"; mk512 "$img" $t
  l=$(sudo losetup -f --show -P --sector-size 4096 "$img"); LOOPS="$LOOPS $l"
  before=$(ls ${l}p* 2>/dev/null | wc -l)
  rep=$(sudo python3 -I "$HERE/guestfix.py" adapt "$l")
  echo "$rep" | python3 -c 'import json,sys; r=json.load(sys.stdin)[0]; print("  action:", r["action"], "| compatible:", r["compatible"], "| problems:", r["problems"])'
  sudo partprobe "$l" 2>/dev/null; sudo blockdev --rereadpt "$l" 2>/dev/null; sleep 1
  after=$(ls ${l}p* 2>/dev/null | wc -l)
  if [ $t = none ]; then
    sudo mount "$l" "$W/m" && { ok "whole-disk ext4 mounts on 4Kn"; sudo umount "$W/m"; } || bad "whole-disk ext4 mount"
    echo "$rep" | grep -q '"action": "none"' && ok "no action" || bad "unexpected action"
    continue
  fi
  [ "$before" = 0 ] && ok "partitions invisible before adapt (512 table on 4Kn)" || echo "  note: $before partitions visible before"
  [ "$after" = 2 ] && ok "2 partitions visible after adapt" || bad "partitions after adapt: $after"
  sudo mount "${l}p1" "$W/m" && { [ "$(cat "$W/m/marker")" = marker ] && ok "ext4 partition mounts, marker intact" || bad "marker"; sudo umount "$W/m"; } || bad "ext4 mount"
  echo "$rep" | grep -q 'xfs sectsz=512 cannot be mounted' && ok "xfs sectsz=512 flagged incompatible" || bad "xfs not flagged"
  sudo mount "${l}p2" "$W/m" 2>/dev/null && { bad "xfs-512 unexpectedly mounted"; sudo umount "$W/m"; } || ok "xfs-512 refuses to mount (as reported)"
  # idempotent: second adapt is a no-op
  sudo python3 -I "$HERE/guestfix.py" adapt "$l" | grep -q '"action": "none"' && ok "second adapt is a no-op" || bad "not idempotent"
  [ $t = gpt ] && { sudo sgdisk -v "$l" 2>&1 | grep -q "No problems found" && ok "sgdisk -v: no problems" || bad "sgdisk -v: $(sudo sgdisk -v "$l" 2>&1 | head -3)"; }
done

echo "== relayout (gpt: ext4 + xfs-512) and (whole-disk xfs-512)"
for t in gpt whole; do
  img="$W/rl-$t.img"; truncate -s 2G "$img"; sp="$W/rl-$t.spare"; truncate -s 2G "$sp"
  l=$(sudo losetup -f --show --sector-size 512 "$img")
  if [ $t = gpt ]; then
    sudo parted -s "$l" mklabel gpt mkpart primary 1MiB 1025MiB mkpart primary 1025MiB 100%; sudo partprobe "$l"; sleep 1
    sudo mkfs.ext4 -q -b 4096 -L rlext "${l}p1"; X="${l}p2"
  else X="$l"; fi
  sudo mkfs.xfs -q -f -s size=512 -L rlxfs "$X"
  sudo mount "$X" "$W/m"
  sudo sh -c "head -c 3M /dev/urandom > $W/m/blob; mkdir $W/m/d; echo hi > $W/m/d/f; ln $W/m/d/f $W/m/d/hard; setfattr -n user.v2v -v kept $W/m/d/f"
  want=$(sudo sh -c "cd $W/m && find . -type f -exec sha256sum {} + | sort" | sha256sum); wuuid=$(sudo blkid -s UUID -o value "$X")
  sudo umount "$W/m"; sudo losetup -d "$l"
  src=$(sudo losetup -f --show -P --sector-size 4096 "$img"); dst=$(sudo losetup -f --show -P --sector-size 4096 "$sp"); LOOPS="$LOOPS $src $dst"
  sudo python3 -I "$HERE/guestfix.py" relayout "$src" "$dst" > "$W/rl.json" 2>&1 || { bad "relayout rc: $(tail -3 $W/rl.json)"; continue; }
  python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print("  steps:", [ (x.get("partition"), x.get("method") or x.get("table")) for x in r["steps"]], "| compatible:", r["compatible"])' "$W/rl.json"
  sudo partprobe "$dst" 2>/dev/null; sleep 1
  D=$([ $t = gpt ] && echo "${dst}p2" || echo "$dst")
  [ "$(sudo blkid -s UUID -o value "$D")" = "$wuuid" ] && ok "$t: same XFS UUID" || bad "$t: UUID changed"
  [ "$(sudo blkid -s LABEL -o value "$D")" = rlxfs ] && ok "$t: same label" || bad "$t: label"
  [ "$(sudo xfs_db -r -c 'sb 0' -c 'p sectsize' "$D" | awk '{print $3}')" = 4096 ] && ok "$t: new sectsz=4096" || bad "$t: sectsz"
  if sudo mount "$D" "$W/m"; then
    got=$(sudo sh -c "cd $W/m && find . -type f -exec sha256sum {} + | sort" | sha256sum)
    [ "$got" = "$want" ] && ok "$t: file contents identical" || bad "$t: contents differ"
    [ "$(sudo stat -c %h $W/m/d/f)" = 2 ] && ok "$t: hardlink kept" || bad "$t: hardlink lost"
    [ "$(sudo getfattr --only-values -n user.v2v $W/m/d/f 2>/dev/null)" = kept ] && ok "$t: xattr kept" || bad "$t: xattr lost"
    sudo umount "$W/m"
  else bad "$t: relaid XFS does not mount on 4Kn"; fi
  if [ $t = gpt ]; then
    sudo mount "${dst}p1" "$W/m" && { ok "gpt: ext4 partition block-copied + mounts"; sudo umount "$W/m"; } || bad "gpt: ext4 copy"
    v512=$(sudo losetup -f --show -r --sector-size 512 "$img"); LOOPS="$LOOPS $v512"   # src table is 512-based
    a=$(sudo sgdisk -i 1 "$v512" | grep 'unique GUID'); b=$(sudo sgdisk -i 1 "$dst" | grep 'unique GUID')
    [ -n "$a" ] && [ "$a" = "$b" ] && ok "gpt: PARTUUID preserved" || bad "gpt: PARTUUID src=[$a] dst=[$b]"
  fi
done

echo "== ntfs (512 B/sector) on 4Kn: BPB rewrite"
img="$W/ntfs.img"; truncate -s 1G "$img"
l=$(sudo losetup -f --show --sector-size 512 "$img")
sudo parted -s "$l" mklabel gpt mkpart primary 1MiB 100%; sudo partprobe "$l"; sleep 1
sudo mkntfs -Q -q -s 512 -c 4096 -L v2vntfs "${l}p1"
sudo ntfs-3g "${l}p1" "$W/m" && sudo sh -c "head -c 5M /dev/urandom > $W/m/blob; mkdir $W/m/d; echo hi > $W/m/d/f" && \
  want=$(sudo sh -c "cd $W/m && find . -type f -exec sha256sum {} + | sort" | sha256sum) && sudo umount "$W/m"
sudo losetup -d "$l"
l=$(sudo losetup -f --show -P --sector-size 4096 "$img"); LOOPS="$LOOPS $l"
sudo mount -t ntfs3 -o ro "${l}p1" "$W/m" 2>/dev/null && { sudo umount "$W/m"; echo "  note: ntfs3 mounted the 512 table?!"; } || true
sudo python3 -I "$HERE/guestfix.py" adapt "$l" > "$W/nt.json"
python3 -c 'import json,sys; r=json.load(open(sys.argv[1]))[0]; print("  action:", r["action"], "| ntfs:", [ (x["partition"], x["action"]) for x in r.get("ntfs",[])], "| compatible:", r["compatible"])' "$W/nt.json"
sudo partprobe "$l" 2>/dev/null; sleep 1
if ! grep -qw ntfs3 /proc/filesystems && ! sudo modprobe ntfs3 2>/dev/null; then
  echo "  skip: this kernel has no ntfs3 (the strict mount check needs it)"
elif sudo mount -t ntfs3 -o ro "${l}p1" "$W/m"; then
  got=$(sudo sh -c "cd $W/m && find . -type f -exec sha256sum {} + | sort" | sha256sum)
  [ "$got" = "$want" ] && ok "ntfs: mounts on 4Kn after BPB rewrite (kernel ntfs3), contents identical" || bad "ntfs: contents differ"
  sudo umount "$W/m"
else bad "ntfs: patched volume does not mount (ntfs3)"; fi
sudo ntfsinfo -m "${l}p1" 2>/dev/null | grep -E "Sector Size|Cluster Size" | sed 's/^/  /'
grep -q '"orig_boot_sector"' "$W/nt.json" && ok "ntfs: original boot sector recorded (revertible)" || bad "ntfs: no revert data"

[ $fail = 0 ] && echo "PASS" || { echo "FAILED"; exit 1; }
