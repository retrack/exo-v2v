#!/usr/bin/env bash
# test_lvm.sh — local test of the LVM handling in guestfix.py (needs sudo, loop
# devices, lvm2, dmsetup, xfsprogs). Disks are built the way a VMware guest has
# them (512-byte sectors) and then seen the way Exoscale shows them:
#   relayout: VG "tvg" over a GPT partition of disk A + the whole disk B, LV span
#     (XFS sectsz=512, across both PVs) and LV e4 (ext4 4K). On 4096-sector loops
#     span is reported as "rebuild", e4 as fine; relayout_group onto two spare
#     4Kn loops; then the VG is activated from the spares only: span is XFS with
#     4K sectors, same fs UUID, same LV UUIDs, markers intact.
#   fold: VG "rvg" over partition 1 of a root disk R (msdos and gpt) + the whole
#     disk F, LV root (XFS-512) across both. R and F are laid into one 512-sector
#     "instance root disk" V (F at a 1 MiB aligned offset after R); fold() adds
#     F as partition 2/3 of V; the VG activates from V alone, marker intact.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/../src/exo_v2v/receiver" && pwd)"
W="$(mktemp -d)"; LOOPS=""; fail=0
cleanup() {
  sudo umount "$W"/m 2>/dev/null
  for vg in tvg rvg; do sudo vgchange -an $vg >/dev/null 2>&1; done
  for l in $LOOPS; do sudo losetup -d $l 2>/dev/null; done
  rm -rf "$W"
}
trap cleanup EXIT
ok() { echo "  ok: $*"; }; bad() { echo "  FAIL: $*"; fail=1; }
mkdir -p "$W/m"
loop() { local l; l=$(sudo losetup -f --show -P "$@"); LOOPS="$LOOPS $l"; echo "$l"; }
unloop() { sudo losetup -d "$1"; LOOPS="${LOOPS/ $1/}"; }
# LVM restricted to the given devices, no devices file, no udev wait
lvmc() { local devs="$1"; shift
  local f; f=$(for d in $devs; do printf '"a|^%s$|",' "$d"; done)
  sudo "$@" --config "devices { use_devicesfile=0 filter=[ ${f} \"r|.*|\" ] } activation { udev_sync=0 udev_rules=0 }"
}
GF() { sudo python3 -I -c "import sys, json; sys.path.insert(0, '$HERE'); import guestfix as G; $1"; }

echo "== relayout: VG across a GPT partition and a whole disk, XFS-512 LV spanning both"
A="$W/a.img"; B="$W/b.img"; truncate -s 1G "$A" "$B"
a=$(loop --sector-size 512 "$A"); b=$(loop --sector-size 512 "$B")
sudo parted -s "$a" mklabel gpt mkpart lvm 1MiB 100% set 1 lvm on; sudo partprobe "$a"; sleep 1
lvmc "${a}p1 $b" pvcreate -q "${a}p1" "$b"
lvmc "${a}p1 $b" vgcreate -q tvg "${a}p1" "$b"
lvmc "${a}p1 $b" lvcreate -q -y -n span -L 1400M tvg
lvmc "${a}p1 $b" lvcreate -q -y -n e4 -l 100%FREE tvg
sudo dmsetup mknodes; sudo mkfs.xfs -q -f /dev/mapper/tvg-span; sudo mkfs.ext4 -q -b 4096 /dev/mapper/tvg-e4
XU=$(sudo blkid -s UUID -o value /dev/mapper/tvg-span)
LU=$(lvmc "${a}p1 $b" lvs --noheadings -o lv_uuid tvg/span | tr -d ' ')
sudo mount /dev/mapper/tvg-span "$W/m"; sudo dd if=/dev/urandom of="$W/m/big" bs=1M count=900 status=none
sudo mkdir "$W/m/dir"; echo hello | sudo tee "$W/m/dir/f" >/dev/null; S1=$(sudo sha256sum "$W/m/big" | cut -d' ' -f1); sudo umount "$W/m"
sudo mount /dev/mapper/tvg-e4 "$W/m"; echo e4 | sudo tee "$W/m/f" >/dev/null; sudo umount "$W/m"
lvmc "${a}p1 $b" vgchange -an -q tvg >/dev/null; unloop "$a"; unloop "$b"
a4=$(loop --sector-size 4096 "$A"); b4=$(loop --sector-size 4096 "$B")
GF "print(json.dumps(G.adapt('$a4')['action']))" | grep -q "rescaled" && ok "GPT of disk A rescaled" || bad "rescale A"
sudo blockdev --rereadpt "$a4"; sleep 1
R=$(GF "r=G.lvm_report(['$a4','$b4']); print(json.dumps([(x['lv'], x['fs'], x['rebuild']) for x in r['lvs']]))")
echo "  lvm: $R"
echo "$R" | grep -q '\["span", "xfs", true\]' && ok "span (xfs-512 on 4Kn) -> rebuild" || bad "span not flagged"
echo "$R" | grep -q '\["e4", "ext", false\]' && ok "e4 (ext4 4K) left as is" || bad "e4 flagged"
G=$(GF "print(json.dumps(G.relayout_groups({'sdb': '$a4', 'sdc': '$b4'})[0]))" 2>/dev/null)
[ "$G" = '[["sdb", "sdc"]]' ] && ok "one relayout group: sdb+sdc" || bad "groups: $G"
SA="$W/sa.img"; SB="$W/sb.img"; truncate -s 1G "$SA" "$SB"
sa=$(loop --sector-size 4096 "$SA"); sb=$(loop --sector-size 4096 "$SB")
OUT=$(GF "r=G.relayout_group({'$a4': '$sa', '$b4': '$sb'}); print(json.dumps({k: (v['compatible'], v['problems'], v['steps']) for k, v in r.items()}))")
echo "$OUT" | python3 -c 'import json,sys; [print("  ", k, v[0], v[1], [s.get("method") for s in v[2]]) for k, v in json.load(sys.stdin).items()]'
echo "$OUT" | grep -q '"problems"' ; echo "$OUT" | python3 -c 'import json,sys; sys.exit(0 if all(v[0] for v in json.load(sys.stdin).values()) else 1)' \
  && ok "relayout reports both volumes compatible" || bad "relayout result"
unloop "$a4"; unloop "$b4"
sudo blockdev --rereadpt "$sa"; sleep 1
lvmc "${sa}p1 $sb" vgchange -ay -q tvg >/dev/null && ok "VG activates from the spares" || bad "vgchange on spares"
sudo dmsetup mknodes
[ "$(lvmc "${sa}p1 $sb" lvs --noheadings -o lv_uuid tvg/span | tr -d ' ')" = "$LU" ] && ok "LV UUID kept" || bad "LV UUID"
sudo mount /dev/mapper/tvg-span "$W/m" && {
  [ "$(sudo blkid -s UUID -o value /dev/mapper/tvg-span)" = "$XU" ] && ok "XFS UUID kept" || bad "XFS UUID"
  sudo xfs_info "$W/m" | grep -q "sectsz=4096" && ok "XFS rebuilt with 4K sectors" || bad "sectsz"
  [ "$(sudo sha256sum "$W/m/big" | cut -d' ' -f1)" = "$S1" ] && [ "$(cat "$W/m/dir/f")" = hello ] && ok "span data intact" || bad "span data"
  sudo umount "$W/m"; } || bad "span does not mount"
sudo mount /dev/mapper/tvg-e4 "$W/m" && { [ "$(cat "$W/m/f")" = e4 ] && ok "e4 data intact (block copy)" || bad "e4 data"; sudo umount "$W/m"; } || bad "e4 mount"
lvmc "${sa}p1 $sb" vgchange -an -q tvg >/dev/null; unloop "$sa"; unloop "$sb"

for t in msdos gpt; do
  echo "== fold ($t root disk): root VG over root-disk partition 1 + whole disk F, XFS-512 root LV"
  R="$W/r-$t.img"; F="$W/f-$t.img"; truncate -s 600M "$R"; truncate -s 300M "$F"
  r=$(loop --sector-size 512 "$R"); f=$(loop --sector-size 512 "$F")
  sudo parted -s "$r" mklabel $t mkpart primary 1MiB 100% set 1 lvm on; sudo partprobe "$r"; sleep 1
  lvmc "${r}p1 $f" pvcreate -q "${r}p1" "$f"; lvmc "${r}p1 $f" vgcreate -q rvg "${r}p1" "$f"
  lvmc "${r}p1 $f" lvcreate -q -y -n root -l 100%FREE rvg; sudo dmsetup mknodes
  sudo mkfs.xfs -q -f /dev/mapper/rvg-root; sudo mount /dev/mapper/rvg-root "$W/m"
  sudo dd if=/dev/urandom of="$W/m/big" bs=1M count=750 status=none; S2=$(sudo sha256sum "$W/m/big" | cut -d' ' -f1); sudo umount "$W/m"
  lvmc "${r}p1 $f" vgchange -an -q rvg >/dev/null; unloop "$r"; unloop "$f"
  V="$W/v-$t.img"; truncate -s 1G "$V"
  dd if="$R" of="$V" conv=notrunc status=none
  OFF=$((600 << 20))             # 600 MiB: already 1 MiB aligned
  dd if="$F" of="$V" bs=1M seek=600 conv=notrunc status=none
  v=$(loop --sector-size 512 "$V")
  OUT=$(GF "print(json.dumps(G.fold('$v', [{'export': 'sdb', 'offset': $OFF, 'size': $((300 << 20))}])))")
  echo "  fold: $OUT"
  sudo partprobe "$v"; sleep 1
  P2=$(ls ${v}p* | sort | tail -1)
  [ "$(ls ${v}p* | wc -l)" = 2 ] && ok "root disk now has 2 partitions" || bad "partitions: $(ls ${v}p*)"
  [ $t = gpt ] && { sudo sgdisk -v "$v" 2>&1 | grep -q "No problems found" && ok "sgdisk -v clean" || bad "sgdisk: $(sudo sgdisk -v "$v" 2>&1 | head -2)"; }
  lvmc "${v}p1 $P2" vgchange -ay -q rvg >/dev/null && ok "root VG activates from the root disk alone" || bad "vgchange"
  sudo dmsetup mknodes
  sudo mount /dev/mapper/rvg-root "$W/m" && {
    [ "$(sudo sha256sum "$W/m/big" | cut -d' ' -f1)" = "$S2" ] && ok "root LV data intact" || bad "root data"
    sudo umount "$W/m"; } || bad "root LV does not mount"
  lvmc "${v}p1 $P2" vgchange -an -q rvg >/dev/null; unloop "$v"
done
[ $fail = 0 ] && echo "ALL OK" || { echo "SOME FAILED"; exit 1; }
