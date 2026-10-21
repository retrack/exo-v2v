#!/usr/bin/env python3
"""Post-copy guest fix-ups the receiver applies around virt-v2v-in-place.

1. Block-size adaptation (4Kn). Exoscale Block Storage volumes present 4096-byte
   logical sectors; VMware disks (and the instance root disk) use 512. Partition
   tables address sectors, so a GPT/MBR written for 512 is invisible on a 4Kn
   volume. `adapt()` rescales GPT and MBR tables in place (every partition must
   be 4 KiB aligned) and reports filesystems that cannot live on 4Kn at all:
   XFS with sectsz < 4096, ext2/3/4 with block size < 4096 (the kernel refuses
   to mount both). `analyze()` is the read-only version (pre-flight).

2. Network re-pointing. VMware NIC names (ens192/ens224/...) and MAC pins do not
   exist on KVM; virt-v2v does not rewrite ifupdown/netplan/NM configs. `netfix()`
   installs a first-boot oneshot (`v2v-netfix.service`, every boot, idempotent)
   that maps stale interface names to the NICs present before networking starts.

CLI (on the receiver, from RAM, disks not mounted):
  guestfix.py analyze /dev/vdb [...]      # JSON report, no writes
  guestfix.py adapt   /dev/vdb [...]      # rescale partition tables when needed
  guestfix.py netfix  /dev/vda            # inject the first-boot network fix
  guestfix.py relayout /dev/vdb /dev/vdc  # rebuild 512-only filesystems onto a spare volume
"""
import json
import os
import struct
import subprocess
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # also under python3 -I
import lvmlayout as L  # noqa: E402

SRC_SECTOR = 512
# Microsoft Reserved Partition: Windows puts it at LBA 34 (not 4 KiB aligned).
# It carries no data on basic disks, so it may be realigned (start rounded up,
# end rounded down) - the only partition type rescale is allowed to shrink.
import uuid as _uuid
MSR_TYPE = _uuid.UUID("E3C9E316-0B5C-4DB8-817D-F92DF00215AE").bytes_le
# Windows disks whose own metadata addresses 512-byte sectors: dynamic disks
# (LDM database) and Storage Spaces. Left untouched on a 4Kn volume, reported.
WIN_FOREIGN = {_uuid.UUID("5808C8AA-7E8F-42E0-85D2-E1E90434CFB3").bytes_le: "Windows dynamic disk (LDM)",
               _uuid.UUID("AF9B60A0-1431-4F62-BC68-3311714A69AD").bytes_le: "Windows dynamic disk (LDM)",
               _uuid.UUID("E75CAF8F-F680-4CEE-AFA3-B001E56EFC2D").bytes_le: "Windows Storage Spaces disk"}
MBR_LDM = 0x42
GPT_SIG = b"EFI PART"
EXTENDED_TYPES = {0x05, 0x0F, 0x85}


# ---------- low-level helpers ----------

def logical_block_size(dev):
    base = os.path.basename(os.path.realpath(dev))
    try:
        return int(open(f"/sys/block/{base}/queue/logical_block_size").read())
    except OSError:  # a regular file (tests) behaves like a 512 device unless told
        return int(os.environ.get("V2V_TEST_LBS", "512"))


def dev_size(dev):
    fd = os.open(dev, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


def pread(dev, off, n):
    fd = os.open(dev, os.O_RDONLY)
    try:
        return os.pread(fd, n, off)
    finally:
        os.close(fd)


def pwrite(dev, off, data):
    fd = os.open(dev, os.O_RDWR)
    try:
        os.pwrite(fd, data, off)
        os.fsync(fd)
    finally:
        os.close(fd)


# ---------- filesystem probes (at a byte offset) ----------

def probe_fs(dev, off):
    """Return (fstype, ok_on_4kn, detail)."""
    head = pread(dev, off, 8192)
    if len(head) < 4096:
        return "short", True, ""
    if head[0:4] == b"XFSB":
        sectsz = struct.unpack_from(">H", head, 102)[0]
        return "xfs", sectsz >= 4096, f"sectsz={sectsz}"
    if struct.unpack_from("<H", head, 1024 + 56)[0] == 0xEF53:
        bs = 1024 << struct.unpack_from("<I", head, 1024 + 24)[0]
        return "ext", bs >= 4096, f"blocksize={bs}"
    if head[3:11] == b"NTFS    ":
        bps = struct.unpack_from("<H", head, 11)[0]
        # Unverified on Windows: a 512-bps NTFS volume on a 4Kn disk is
        # expected to fail to mount; flagged until T4 settles it.
        return "ntfs", bps >= 4096, f"bytes_per_sector={bps}"
    if head[512:520] == b"LABELONE":
        return "lvm2-pv", True, "PV label ok on 4Kn; LV contents not inspected"
    if head[4086:4096] == b"SWAPSPACE2":
        return "swap", True, ""
    if head[0:4] == b"\x00\x00\x00\x00" and not any(head[:4096]):
        return "empty", True, ""
    return "unknown", True, "not recognized"


# ---------- partition tables ----------

def read_gpt(dev, ss):
    """Parse a GPT whose header sits at LBA 1 of sector size ss; None if absent."""
    hdr = pread(dev, ss, 92)
    if hdr[:8] != GPT_SIG:
        return None
    (rev, hsz, hcrc, _r, my_lba, alt_lba, first_u, last_u) = struct.unpack_from("<IIIIQQQQ", hdr, 8)
    disk_guid = hdr[56:72]
    ent_lba, n_ent, ent_sz, ent_crc = struct.unpack_from("<QIII", hdr, 72)
    raw = pread(dev, ent_lba * ss, n_ent * ent_sz)
    parts = []
    for i in range(n_ent):
        e = raw[i * ent_sz:(i + 1) * ent_sz]
        if e[:16] != b"\x00" * 16:
            first, last = struct.unpack_from("<QQ", e, 32)
            parts.append({"index": i, "first": first, "last": last, "msr": e[:16] == MSR_TYPE,
                          "foreign": WIN_FOREIGN.get(bytes(e[:16]))})
    return {"rev": rev, "hsz": hsz, "disk_guid": disk_guid, "n_ent": n_ent,
            "ent_sz": ent_sz, "entries": bytearray(raw), "parts": parts,
            "first_usable": first_u, "last_usable": last_u}


def read_mbr(dev):
    mbr = pread(dev, 0, 512)
    if mbr[510:512] != b"\x55\xaa":
        return None
    parts = []
    for i in range(4):
        e = mbr[446 + 16 * i: 446 + 16 * (i + 1)]
        ptype = e[4]
        start, count = struct.unpack_from("<II", e, 8)
        if ptype and count:
            parts.append({"index": i, "type": ptype, "start": start, "count": count})
    return {"raw": bytearray(mbr), "parts": parts}


def mbr_unit(dev, m):
    """An MBR does not record its sector size: infer it from where the first
    recognizable filesystem signature sits (default: the device's own size)."""
    lbs = logical_block_size(dev)
    size = dev_size(dev)
    prims = [p for p in m["parts"] if p["type"] not in EXTENDED_TYPES]
    for unit in (lbs, 512 if lbs != 512 else 4096):
        for p in prims:
            off = p["start"] * unit
            if (p["start"] + p["count"]) * unit <= size and probe_fs(dev, off)[0] not in ("unknown", "short", "empty"):
                return unit
    return lbs


def layout(dev):
    """('gpt', ss, table) | ('mbr', 512, table) | ('none', 0, None)."""
    for ss in (4096, 512):
        g = read_gpt(dev, ss)
        if g:
            return "gpt", ss, g
    m = read_mbr(dev)
    if m and not any(p["type"] == 0xEE for p in m["parts"]):
        return "mbr", mbr_unit(dev, m), m
    if m:  # protective MBR without a readable GPT
        return "gpt-broken", 0, None
    return "none", 0, None


def partition_offsets(kind, ss, table):
    if kind == "gpt":
        return [(p["index"] + 1, p["first"] * ss, (p["last"] + 1 - p["first"]) * ss) for p in table["parts"]]
    if kind == "mbr":
        return [(p["index"] + 1, p["start"] * ss, p["count"] * ss) for p in table["parts"]
                if p["type"] not in EXTENDED_TYPES]
    return []


def analyze(dev):
    lbs = logical_block_size(dev)
    kind, ss, table = layout(dev)
    rep = {"dev": dev, "logical_block_size": lbs, "layout": kind, "table_sector": ss,
           "needs_rescale": lbs == 4096 and kind in ("gpt", "mbr") and ss == 512,
           "filesystems": [], "compatible": True, "problems": []}
    if kind == "gpt-broken":
        rep["problems"].append("protective MBR but no GPT header at 512 or 4096")
    if kind == "mbr" and any(p["type"] in EXTENDED_TYPES for p in table["parts"]):
        rep["problems"].append("MBR extended/logical partitions are not rescaled")
    foreign = sorted({p["foreign"] for p in table["parts"] if p.get("foreign")} if kind == "gpt" else
                     {"Windows dynamic disk (LDM)"} if kind == "mbr" and any(p["type"] == MBR_LDM
                                                                             for p in table["parts"]) else set())
    if foreign:
        # its metadata counts 512-byte sectors: rewriting the partition table or
        # an NTFS boot sector would not make it readable - leave the volume as it is
        rep.update(foreign=foreign[0], needs_rescale=False, compatible=False)
        if lbs == 4096:
            rep["problems"].append(f"{foreign[0]}: its metadata addresses 512-byte sectors and cannot be "
                                   "moved to a 4Kn volume; left untouched (convert to a basic disk before "
                                   "migrating; a spanned or striped volume must be copied to a basic volume)")
        return rep
    places = partition_offsets(kind, ss, table) if kind in ("gpt", "mbr") else [(0, 0, dev_size(dev))]
    msr = {p["index"] + 1 for p in table["parts"] if p.get("msr")} if kind == "gpt" else set()
    for num, off, size in places:
        if lbs == 4096 and off % 4096 and num not in msr:
            rep["problems"].append(f"partition {num} starts at byte {off}, not 4 KiB aligned")
        fstype, ok, detail = probe_fs(dev, off)
        rep["filesystems"].append({"partition": num, "offset": off, "size": size,
                                   "type": fstype, "ok_on_4kn": ok, "detail": detail})
        if lbs == 4096 and not ok:
            rep["problems"].append(f"partition {num}: {fstype} {detail} cannot be mounted on a 4Kn volume")
    rep["compatible"] = not rep["problems"]
    return rep


# ---------- rescale 512 -> 4096 ----------

def _ceil8(n):
    return (n + 7) // 8


def rescale_gpt(dev, g):
    S = 4096
    total = dev_size(dev) // S
    ent_bytes = g["n_ent"] * g["ent_sz"]
    ent_secs = (ent_bytes + S - 1) // S
    first_usable, last_usable = 2 + ent_secs, total - 2 - ent_secs
    ents = g["entries"]
    spans = []
    for p in g["parts"]:
        if p["first"] % 8 and not p.get("msr"):
            raise ValueError(f"GPT partition {p['index'] + 1} start LBA {p['first']} not 4 KiB aligned")
        if p.get("msr"):   # realign inward: no data to preserve
            nf, nl = max(_ceil8(p["first"]), first_usable), (p["last"] + 1) // 8 - 1
        else:
            nf, nl = p["first"] // 8, _ceil8(p["last"] + 1) - 1
        if nf < first_usable:
            raise ValueError(f"GPT partition {p['index'] + 1} starts inside the 4Kn GPT header area")
        if nl > last_usable:
            raise ValueError(f"GPT partition {p['index'] + 1} reaches the last {(total - last_usable) * S // 1024} KiB "
                             "of the volume, needed by the 4Kn backup GPT: the volume must be >= 1 MiB "
                             "larger than the source disk")
        spans.append((nf, nl))
        struct.pack_into("<QQ", ents, p["index"] * g["ent_sz"] + 32, nf, nl)
    spans.sort()
    for (a, b), (c, _d) in zip(spans, spans[1:]):
        if b >= c:
            raise ValueError("rounded partitions would overlap")
    ent_crc = zlib.crc32(bytes(ents)) & 0xFFFFFFFF

    def header(my, alt, ent_lba):
        h = bytearray(92)
        h[:8] = GPT_SIG
        struct.pack_into("<IIIIQQQQ", h, 8, g["rev"], 92, 0, 0, my, alt, first_usable, last_usable)
        h[56:72] = g["disk_guid"]
        struct.pack_into("<QIII", h, 72, ent_lba, g["n_ent"], g["ent_sz"], ent_crc)
        struct.pack_into("<I", h, 16, zlib.crc32(bytes(h)) & 0xFFFFFFFF)
        return bytes(h) + b"\x00" * (S - 92)

    mbr = bytearray(pread(dev, 0, 512))
    pm = bytearray(16)
    pm[4] = 0xEE
    struct.pack_into("<II", pm, 8, 1, min(total - 1, 0xFFFFFFFF))
    pm[1:4] = b"\x00\x02\x00"; pm[5:8] = b"\xff\xff\xff"
    mbr[446:510] = bytes(pm) + b"\x00" * 48
    mbr[510:512] = b"\x55\xaa"
    ents_padded = bytes(ents) + b"\x00" * (ent_secs * S - ent_bytes)
    # primary: LBA0 pMBR, LBA1 header, LBA2.. entries (overwrites the old 512 GPT)
    pwrite(dev, 0, bytes(mbr) + b"\x00" * (S - 512))
    pwrite(dev, S, header(1, total - 1, 2))
    pwrite(dev, 2 * S, ents_padded)
    # backup: entries then header in the last sectors (covers the old backup GPT)
    pwrite(dev, (total - 1 - ent_secs) * S, ents_padded)
    pwrite(dev, (total - 1) * S, header(total - 1, 1, total - 1 - ent_secs))
    return [{"partition": p["index"] + 1, "first": struct.unpack_from("<Q", ents, p["index"] * g["ent_sz"] + 32)[0],
             "last": struct.unpack_from("<Q", ents, p["index"] * g["ent_sz"] + 40)[0]} for p in g["parts"]]


def rescale_mbr(dev, m):
    raw = m["raw"]
    total = dev_size(dev) // 4096
    spans = []
    for p in m["parts"]:
        if p["type"] in EXTENDED_TYPES:
            raise ValueError("MBR extended partitions are not supported")
        if p["start"] % 8:
            raise ValueError(f"MBR partition {p['index'] + 1} start {p['start']} not 4 KiB aligned")
        ns, nc = p["start"] // 8, _ceil8(p["count"])
        if ns + nc > total:
            raise ValueError(f"MBR partition {p['index'] + 1} beyond end of disk after rounding")
        spans.append((ns, ns + nc, p["index"]))
        struct.pack_into("<II", raw, 446 + 16 * p["index"] + 8, ns, nc)
    spans.sort()
    for (a, b, _i), (c, _d, _j) in zip(spans, spans[1:]):
        if b > c:
            raise ValueError("rounded MBR partitions would overlap")
    pwrite(dev, 0, bytes(raw))
    return [{"partition": i + 1, "start": a, "count": b - a} for a, b, i in spans]


def _fix_ntfs(dev, rep, cur):
    """Patch 512-byte-sector NTFS volumes (see ntfs_rebps); returns a fresh analysis."""
    if os.environ.get("V2V_NTFS_REBPS", "1") != "1":
        return cur
    kind, ss, table = layout(dev)
    places = partition_offsets(kind, ss, table) if kind in ("gpt", "mbr") else [(0, 0, dev_size(dev))]
    done = []
    for (num, off, size), fs in zip(places, cur["filesystems"]):
        if fs["type"] == "ntfs" and not fs["ok_on_4kn"]:
            try:
                done.append({"partition": num, **ntfs_rebps(dev, off, size)})
            except ValueError as e:
                done.append({"partition": num, "action": "failed", "error": str(e)})
    if done:
        rep["ntfs"] = done
    return analyze(dev)


def adapt(dev):
    rep = analyze(dev)
    rep["action"] = "none"
    if rep.get("foreign"):
        return rep
    if not rep["needs_rescale"]:
        if rep["logical_block_size"] == 4096:
            after = _fix_ntfs(dev, rep, rep)
            rep.update(problems=after["problems"], compatible=after["compatible"], filesystems=after["filesystems"])
        return rep
    kind, ss, table = layout(dev)
    try:
        rep["rescaled"] = rescale_gpt(dev, table) if kind == "gpt" else rescale_mbr(dev, table)
        rep["action"] = f"{kind} rescaled 512->4096"
        subprocess.call(["blockdev", "--rereadpt", dev], stderr=subprocess.DEVNULL)
    except ValueError as e:
        rep["action"] = "failed"
        rep["problems"].append(f"rescale: {e}")
        rep["compatible"] = False
        return rep
    after = analyze(dev)
    rep.update(layout=after["layout"], table_sector=after["table_sector"],
               filesystems=after["filesystems"], needs_rescale=after["needs_rescale"])
    after = _fix_ntfs(dev, rep, after)
    rep["problems"] = after["problems"]
    rep["compatible"] = after["compatible"]
    return rep


# ---------- NTFS: re-declare the volume geometry for 4096-byte sectors ----------

def ntfs_rebps(dev, off, size, new_bps=4096):
    """An NTFS volume formatted with 512 bytes/sector shows up RAW on a 4Kn disk
    (verified with Windows Server 2022 on Exoscale). Everything in NTFS is
    addressed in clusters except the BPB: bytes/sector, sectors/cluster, total
    sectors, hidden sectors, and the backup boot sector (last sector of the
    volume). With clusters >= 4 KiB nothing moves: patch the BPB, write the backup
    boot sector into the last 4 KiB sector (outside every cluster when the
    partition size is a multiple of 4 KiB). Returns the original boot sector
    (hex) so the change can be reverted."""
    bs = bytearray(pread(dev, off, 512))
    if bs[3:11] != b"NTFS    ":
        raise ValueError("not NTFS")
    bps = struct.unpack_from("<H", bs, 11)[0]
    raw_spc = bs[13]
    spc = (1 << (256 - raw_spc)) if raw_spc > 0x80 else raw_spc
    cluster = bps * spc
    if bps == new_bps:
        return {"action": "none"}
    if cluster < new_bps or cluster % new_bps or size % new_bps:
        raise ValueError(f"cluster size {cluster} B / partition size {size} not compatible with {new_bps} B sectors")
    old_total = struct.unpack_from("<Q", bs, 0x28)[0]
    if (old_total + 1) * bps > size:
        raise ValueError("NTFS volume larger than its partition")
    orig = bytes(bs)
    new_total = size // new_bps - 1
    struct.pack_into("<H", bs, 11, new_bps)
    bs[13] = cluster // new_bps
    struct.pack_into("<Q", bs, 0x28, new_total)
    struct.pack_into("<I", bs, 0x1C, (off // new_bps) & 0xFFFFFFFF)   # hidden sectors
    pwrite(dev, off, bytes(bs))
    pwrite(dev, off + new_total * new_bps, bytes(bs))                  # backup boot sector
    return {"action": f"ntfs bps {bps}->{new_bps}", "cluster": cluster, "total_sectors": new_total,
            "orig_boot_sector": orig.hex()}


# ---------- relayout: rebuild 4Kn-incompatible filesystems on a spare volume ----------

REBUILDABLE = ("xfs", "ext")


def _run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode:
        raise RuntimeError(f"{' '.join(cmd)}: rc={r.returncode} {r.stderr.strip()[-300:]}")
    return r.stdout


def _need(cmd, pkg):
    """A tool the receiver may lack (an older receiver template): install it."""
    if subprocess.call(["which", cmd], stdout=subprocess.DEVNULL) == 0:
        return
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    for step in (["apt-get", "update", "-q"], ["apt-get", "install", "-y", "-q", "--no-install-recommends", pkg]):
        subprocess.call(step, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if subprocess.call(["which", cmd], stdout=subprocess.DEVNULL):
        raise RuntimeError(f"{cmd} is missing on the receiver (apt-get install {pkg} failed)")


def _loop512(dev, off, size):
    """Read-only 512-sector view of a byte range of a 4Kn device (mountable XFS-512)."""
    return _run(["losetup", "-f", "--show", "-r", "--sector-size", "512",
                 "-o", str(off), "--sizelimit", str(size), dev]).strip()


def _blkid(dev):
    out = _run(["blkid", "-p", "-o", "export", dev]) if os.path.exists(dev) else ""
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)


def _copy_range(src, dst, off, size, chunk=8 << 20, skip=()):
    """Byte copy of [off, off+size) skipping all-zero chunks (dst is a fresh volume)
    and the byte ranges in `skip` [(offset, length)] (rebuilt separately)."""
    todo, pos = [], off
    for s, n in sorted((s, n) for s, n in skip if s < off + size and s + n > off):
        if s > pos:
            todo.append((pos, s - pos))
        pos = max(pos, s + n)
    if pos < off + size:
        todo.append((pos, off + size - pos))
    fi, fo = os.open(src, os.O_RDONLY), os.open(dst, os.O_WRONLY)
    try:
        zero = bytes(chunk)
        for start, length in todo:
            p = 0
            while p < length:
                n = min(chunk, length - p)
                b = os.pread(fi, n, start + p)
                if b != zero[:len(b)]:
                    os.pwrite(fo, b, start + p)
                p += n
        os.fsync(fo)
    finally:
        os.close(fi); os.close(fo)


def _part_dev(dev, num):
    base = os.path.realpath(dev)
    return f"{base}p{num}" if base[-1].isdigit() else f"{base}{num}"


def rebuild_fs(old, new, fstype, workdir="/run/v2v-relayout"):
    """Recreate the filesystem of `old` (a 512-sector view) on `new` with 4 KiB
    sectors/blocks and the SAME UUID/label, then copy the files (xfsdump|xfsrestore
    for XFS, rsync -aAXHS otherwise). `old` is mounted read-only."""
    os.makedirs(f"{workdir}/src", exist_ok=True); os.makedirs(f"{workdir}/dst", exist_ok=True)
    ids = _blkid(old)
    uuid, label = ids.get("UUID", ""), ids.get("LABEL", "")
    if fstype == "xfs":
        cmd = ["mkfs.xfs", "-f", "-s", "size=4096"] + (["-m", f"uuid={uuid}"] if uuid else []) + \
              (["-L", label] if label else []) + [new]
    else:
        cmd = ["mkfs.ext4", "-F", "-q", "-b", "4096"] + (["-U", uuid] if uuid else []) + \
              (["-L", label] if label else []) + [new]
    _run(cmd)
    # XFS: norecovery (ro view, a dirty log cannot be replayed) + nouuid
    # (the new fs carries the same UUID); ext: noload skips the journal.
    opts = "ro,norecovery,nouuid" if fstype == "xfs" else "ro,noload"
    _run(["mount", "-o", opts, old, f"{workdir}/src"])
    try:
        _run(["mount", "-o", "nouuid" if fstype == "xfs" else "defaults", new, f"{workdir}/dst"])
        try:
            if fstype == "xfs" and subprocess.call(["which", "xfsdump"], stdout=subprocess.DEVNULL) == 0:
                _run(["sh", "-c", f"xfsdump -q -J -l 0 - {workdir}/src | xfsrestore -q -J - {workdir}/dst"])
                method = "mkfs.xfs 4K + xfsdump|xfsrestore"
            else:
                _run(["rsync", "-aAXHS", "--numeric-ids", f"{workdir}/src/", f"{workdir}/dst/"])
                method = "mkfs 4K + rsync"
            used = _run(["df", "--output=used", "-B1", f"{workdir}/src"]).split()[-1]
        finally:
            _run(["umount", f"{workdir}/dst"])
    finally:
        _run(["umount", f"{workdir}/src"])
    return {"method": method, "uuid": uuid, "label": label, "bytes_used": int(used)}


def relayout(src, spare, workdir="/run/v2v-relayout"):
    """One volume: see relayout_group."""
    return relayout_group({src: spare}, workdir=workdir)[src]


def relayout_group(pairs, others=(), workdir="/run/v2v-relayout"):
    """Rebuild 4Kn data volumes holding 512-sector-only filesystems onto empty
    4Kn spare volumes of the same size (pairs: {src: spare}). A volume group with
    PVs on several of the volumes is rebuilt across them together.
    - Partition table (incl. PARTUUIDs) copied, then rescaled.
    - Compatible partitions and PVs copied byte-for-byte (so VG/PV/LV UUIDs and
      the extent layout stay as they are).
    - XFS sectsz<4096 / ext block<4096, as a partition or a logical volume:
      recreated with 4K sectors and the SAME UUID/label, then filled from a
      read-only 512-sector view of the source (rebuild_fs).
    `others`: devices that are not rebuilt but may hold PVs of the same VGs (the
    root disk); a VG with a PV there is refused (it must be folded instead).
    Returns {src: report}; the sources are never written."""
    for src, spare in pairs.items():
        if dev_size(spare) < dev_size(src):
            raise ValueError(f"spare {spare} is smaller than {src}")
        if logical_block_size(spare) != 4096:
            raise ValueError(f"spare {spare} is not a 4Kn device")
    lv = lvm_analyze(list(pairs) + list(others))
    reps, plan_ = {}, {}
    for src in pairs:
        rep = analyze(src)
        kind, ss, table = layout(src)
        places = partition_offsets(kind, ss, table) if kind in ("gpt", "mbr") else [(0, 0, dev_size(src))]
        # only filesystems we can recreate faithfully; anything else (NTFS, LVs
        # of other types, unknown) is refused BEFORE any spare is touched
        bad = [f for f in rep["filesystems"] if not f["ok_on_4kn"] and f["type"] not in REBUILDABLE]
        bad += lv["unfixable"].get(src, [])
        if bad:
            raise ValueError(f"{src}: cannot rebuild " + ", ".join(
                f"partition {f['partition']} ({f['type']} {f['detail']})" if isinstance(f, dict) else f for f in bad))
        reps[src], plan_[src] = rep, (kind, places)
    rebuild_lvs = [x for x in lv["lvs"] if x["rebuild"] and set(x["devs"]) & set(pairs)]
    for x in rebuild_lvs:
        if not set(x["devs"]) <= set(pairs):
            raise ValueError(f"LV {x['vg']}/{x['lv']} also uses {sorted(set(x['devs']) - set(pairs))}: "
                             "rebuild those volumes in the same group")
    skip = {src: [] for src in pairs}
    for x in rebuild_lvs:
        for dev, off, n in x["extents"]:
            skip[dev].append((off, n))
    out = {}
    for src, spare in pairs.items():
        kind, places = plan_[src]
        rep, steps = reps[src], []
        if kind in ("gpt", "mbr"):
            # table region + tail (backup GPT) byte-for-byte, then rescale on the spare
            first = min(off for _n, off, _s in places)
            end = max(off + size for _n, off, size in places)
            _copy_range(src, spare, 0, first)
            if dev_size(src) > end:
                _copy_range(src, spare, end, dev_size(src) - end)
            a = adapt(spare)
            steps.append({"table": a["action"]})
            subprocess.call(["udevadm", "settle"])
        for (num, off, size), fs in zip(places, rep["filesystems"]):
            dst = spare if num == 0 else _part_dev(spare, num)
            if fs["ok_on_4kn"]:
                _copy_range(src, spare, off, size, skip=skip[src])
                steps.append({"partition": num, "type": fs["type"],
                              "method": "block copy" + (" (PV: header + kept LVs)" if fs["type"] == "lvm2-pv" else "")})
                continue
            loop = _loop512(src, off, size)
            try:
                steps.append({"partition": num, "type": fs["type"], "detail": fs["detail"],
                              **rebuild_fs(loop, dst, fs["type"], workdir)})
            finally:
                subprocess.call(["losetup", "-d", loop])
        out[src] = {"src": src, "spare": spare, "steps": steps}
    # logical volumes: old = dm over read-only 512-sector loop views of the
    # sources, new = dm over the spares (same offsets: same table, same PVs)
    if rebuild_lvs:
        _need("dmsetup", "dmsetup")
    loops = {}
    try:
        for src in {d for x in rebuild_lvs for d in x["devs"]}:
            loops[src] = _run(["losetup", "-f", "--show", "-r", "--sector-size", "512", src]).strip()
        for i, x in enumerate(rebuild_lvs):
            lvd = x["layout"]
            old = L.dm_create(f"v2v-old-{i}", L.dm_table(lvd, loops), readonly=True)
            try:
                new = L.dm_create(f"v2v-new-{i}", L.dm_table(lvd, pairs))
                try:
                    r = rebuild_fs(old, new, x["fs"], workdir)
                finally:
                    L.dm_remove(f"v2v-new-{i}")
            finally:
                L.dm_remove(f"v2v-old-{i}")
            for dev in x["devs"]:
                out[dev]["steps"].append({"lv": f"{x['vg']}/{x['lv']}", "type": x["fs"], "detail": x["detail"], **r})
    finally:
        for l in loops.values():
            subprocess.call(["losetup", "-d", l])
    _run(["sync"])
    after_lvm = lvm_analyze(list(pairs.values()) + list(others))
    for src, spare in pairs.items():
        after = analyze(spare)
        probs = after["problems"] + after_lvm["problems"].get(spare, [])
        out[src].update(compatible=not probs, problems=probs, filesystems=after["filesystems"])
    return out


# ---------- fold: a source disk carried inside the system disk ----------
#
# A VM disk that shares a filesystem with the system disk (a root volume group
# extended onto a second VMDK) cannot become a 4Kn Block Storage volume: the LV
# would span 512- and 4096-byte-sector devices and a 512-only filesystem on it
# (XFS sectsz=512, the RHEL default) no longer mounts. Such a disk is replicated
# into a region of the instance's root disk (512-byte sectors) instead, and at
# cutover its partitions (or the whole disk) become partitions of the root disk.

GPT_LVM = _uuid.UUID("E6D6D379-F507-44C2-A23C-238F2A3DF928").bytes_le
GPT_LINUX = _uuid.UUID("0FC63DAF-8483-4772-8E79-3D69D8477DE4").bytes_le


def _slice_parts(dev, off, size):
    """Partitions of the source disk held in [off, off+size) of dev, as absolute
    (offset, size, mbr_type, gpt_type, gpt_guid, gpt_name) - the whole region when
    it has no partition table."""
    loop = _run(["losetup", "-f", "--show", "-r", "-o", str(off), "--sizelimit", str(size), dev]).strip()
    try:
        kind, ss, table = layout(loop)
        if kind == "gpt":
            out = []
            for p in table["parts"]:
                e = table["entries"][p["index"] * table["ent_sz"]:(p["index"] + 1) * table["ent_sz"]]
                t = bytes(e[:16])
                out.append((off + p["first"] * ss, (p["last"] + 1 - p["first"]) * ss,
                            0x8E if t == GPT_LVM else 0x83, t, bytes(e[16:32]), bytes(e[56:128])))
            return out
        if kind == "mbr":
            return [(off + p["start"] * ss, p["count"] * ss, p["type"],
                     GPT_LVM if p["type"] == 0x8E else GPT_LINUX, _uuid.uuid4().bytes_le, b"")
                    for p in table["parts"] if p["type"] not in EXTENDED_TYPES]
        fstype = probe_fs(loop, 0)[0]
        return [(off, size, 0x8E if fstype == "lvm2-pv" else 0x83,
                 GPT_LVM if fstype == "lvm2-pv" else GPT_LINUX, _uuid.uuid4().bytes_le, b"")]
    finally:
        subprocess.call(["losetup", "-d", loop])


def _gpt_write(dev, g, ents, S=512):
    """Write a GPT (primary + backup at the end of the device) with entries `ents`."""
    total = dev_size(dev) // S
    ent_bytes = g["n_ent"] * g["ent_sz"]
    ent_secs = (ent_bytes + S - 1) // S
    first_usable, last_usable = 2 + ent_secs, total - 2 - ent_secs
    ent_crc = zlib.crc32(bytes(ents)) & 0xFFFFFFFF

    def header(my, alt, ent_lba):
        h = bytearray(92)
        h[:8] = GPT_SIG
        struct.pack_into("<IIIIQQQQ", h, 8, g["rev"], 92, 0, 0, my, alt, first_usable, last_usable)
        h[56:72] = g["disk_guid"]
        struct.pack_into("<QIII", h, 72, ent_lba, g["n_ent"], g["ent_sz"], ent_crc)
        struct.pack_into("<I", h, 16, zlib.crc32(bytes(h)) & 0xFFFFFFFF)
        return bytes(h) + b"\x00" * (S - 92)

    padded = bytes(ents) + b"\x00" * (ent_secs * S - ent_bytes)
    pwrite(dev, S, header(1, total - 1, 2))
    pwrite(dev, 2 * S, padded)
    pwrite(dev, (total - 1 - ent_secs) * S, padded)
    pwrite(dev, (total - 1) * S, header(total - 1, 1, total - 1 - ent_secs))
    return last_usable


def fold(root, folds):
    """Add the partitions of each folded disk (folds: [{"export", "offset",
    "size"}], byte regions of `root`) to the root disk's partition table."""
    if logical_block_size(root) != 512:
        raise ValueError(f"{root} is not a 512-byte-sector disk")
    kind, ss, table = layout(root)
    if kind not in ("gpt", "mbr"):
        raise ValueError(f"system disk has no partition table ({kind}): cannot fold disks into it")
    new = []
    for f in folds:
        for p in _slice_parts(root, f["offset"], f["size"]):
            if p[0] % 4096 or p[1] % 512:
                raise ValueError(f"{f['export']}: partition at byte {p[0]} is not aligned")
            new.append((f["export"], *p))
    used = partition_offsets(kind, ss, table)
    for exp, off, size, *_ in new:
        for num, uo, us in used:
            if off < uo + us and uo < off + size:
                raise ValueError(f"{exp}: region overlaps partition {num} of the system disk")
    added = []
    if kind == "mbr":
        raw = table["raw"]
        free = [i for i in range(4) if not raw[446 + 16 * i + 4]]
        if len(free) < len(new):
            raise ValueError(f"MBR has {len(free)} free primary slots, {len(new)} needed")
        for (exp, off, size, mtype, *_), i in zip(new, free):
            e = bytearray(16)
            e[1:4] = b"\xfe\xff\xff"; e[4] = mtype; e[5:8] = b"\xfe\xff\xff"
            struct.pack_into("<II", e, 8, off // 512, size // 512)
            raw[446 + 16 * i:446 + 16 * (i + 1)] = e
            added.append({"export": exp, "partition": i + 1, "offset": off, "size": size, "type": hex(mtype)})
        pwrite(root, 0, bytes(raw))
    else:
        if ss != 512:
            raise ValueError("system disk GPT is not 512-byte")
        ents = table["entries"]
        n = table["ent_sz"]
        free = [i for i in range(table["n_ent"]) if ents[i * n:i * n + 16] == b"\x00" * 16]
        if len(free) < len(new):
            raise ValueError("GPT has no free entries")
        end = dev_size(root) // 512 - 34
        for (exp, off, size, _mt, gtype, gguid, gname), i in zip(new, free):
            if (off + size) // 512 - 1 > end:
                raise ValueError(f"{exp}: region reaches the backup GPT area")
            e = bytearray(n)
            e[0:16] = gtype; e[16:32] = gguid
            struct.pack_into("<QQ", e, 32, off // 512, (off + size) // 512 - 1)
            e[56:56 + len(gname)] = gname[:n - 56]
            ents[i * n:(i + 1) * n] = e
            added.append({"export": exp, "partition": i + 1, "offset": off, "size": size,
                          "type": str(_uuid.UUID(bytes_le=bytes(gtype)))})
        _gpt_write(root, table, ents)
    subprocess.call(["blockdev", "--rereadpt", root], stderr=subprocess.DEVNULL)
    subprocess.call(["udevadm", "settle"])
    return {"table": kind, "added": added}


# ---------- LVM: volume groups across the guest's disks ----------


def pv_places(dev):
    """[(dev, offset, size)] of the partitions (or the whole disk) of a device."""
    kind, ss, table = layout(dev)
    if kind in ("gpt", "mbr"):
        return [(dev, off, size) for _n, off, size in partition_offsets(kind, ss, table)]
    return [(dev, 0, dev_size(dev))]


def lvm_analyze(devs):
    """Volume groups on `devs` and, per LV, whether it can be read where it is:
    an LV with a 512-only filesystem (XFS sectsz<4096, ext block<4096) that
    touches a 4Kn device cannot be mounted. Returns {"vgs", "lvs", "problems":
    {dev: [...]}}; an LV to rebuild has rebuild=True."""
    places = [p for d in devs for p in pv_places(d)]
    vgs = L.scan(places)
    lbs = {d: logical_block_size(d) for d in devs}
    lvs, problems, unfixable = [], {}, {}
    for vg in vgs.values():
        pv_devs = sorted({p["dev"] for p in vg["pvs"].values() if p["dev"]})
        mixed = len({lbs.get(d) for d in pv_devs}) > 1
        for name, x in vg["lvs"].items():
            item = {"vg": vg["name"], "lv": name, "types": x["types"], "devs": x["devs"], "rebuild": False,
                    "fs": None, "detail": "", "extents": [], "layout": x}
            lvs.append(item)
            if not x["devs"] or all(lbs.get(d) != 4096 for d in x["devs"]):
                continue
            if x["segments"] is None or not x["complete"]:
                dev, off = None, None
            else:
                dev, off = L.lv_start(x)
            if dev is None:
                fstype, ok, detail = "unknown", True, f"LV type {'/'.join(x['types'])} not inspected"
            else:
                fstype, ok, detail = probe_fs(dev, off)
                item["extents"] = [(leg["dev"], leg["offset"], s["length"] // len(s["legs"]))
                                   for s in x["segments"] for leg in s["legs"]]
            item.update(fs=fstype, detail=detail)
            if ok:
                continue
            where = ", ".join(x["devs"])
            if mixed and any(lbs.get(d) == 512 for d in pv_devs):
                msg = (f"LV {vg['name']}/{name} ({fstype} {detail}) spans the 512-sector system disk and 4Kn "
                       f"volumes ({where}): cannot be mounted; fold the volumes into the system disk")
            elif fstype in REBUILDABLE and x["segments"] is not None:
                item["rebuild"] = True
                msg = f"LV {vg['name']}/{name}: {fstype} {detail} cannot be mounted on 4Kn ({where}): rebuild"
            else:
                msg = f"LV {vg['name']}/{name}: {fstype} {detail} on 4Kn ({where}) and cannot be rebuilt"
            for d in x["devs"]:
                problems.setdefault(d, []).append(msg)
                if not item["rebuild"]:
                    unfixable.setdefault(d, []).append(msg)
    return {"vgs": {k: {"name": v["name"], "complete": v["complete"],
                        "pvs": {n: {kk: p[kk] for kk in ("id", "dev", "offset", "pe_start", "pe_count")}
                                for n, p in v["pvs"].items()}} for k, v in vgs.items()},
            "lvs": lvs, "problems": problems, "unfixable": unfixable}


def lvm_report(devs):
    """lvm_analyze without the internal layouts (JSON for the API)."""
    r = lvm_analyze(devs)
    r["lvs"] = [{k: v for k, v in x.items() if k not in ("layout", "extents")} for x in r["lvs"]]
    return r


def relayout_groups(exports, root="sda"):
    """Data exports that must be rebuilt together: [[export, ...], ...] - one per
    set of volumes linked by an LV to rebuild, plus single volumes with 512-only
    partitions. Also returns the problems no relayout can fix."""
    data = {k: v for k, v in exports.items() if k != root}
    by_dev = {v: k for k, v in data.items()}
    lv = lvm_analyze(list(exports.values()))
    groups, unfixable = [], {}
    for k, dev in data.items():
        rep = analyze(dev)
        if rep.get("foreign"):
            unfixable.setdefault(k, []).extend(rep["problems"])
            continue
        if any(not f["ok_on_4kn"] and f["type"] in REBUILDABLE for f in rep["filesystems"]):
            groups.append({k})
        for f in rep["filesystems"]:
            if not f["ok_on_4kn"] and f["type"] not in REBUILDABLE:
                unfixable.setdefault(k, []).append(f"partition {f['partition']}: {f['type']} {f['detail']}")
    for x in lv["lvs"]:
        if x["rebuild"]:
            groups.append({by_dev[d] for d in x["devs"] if d in by_dev})
    for d, probs in lv["unfixable"].items():
        if d in by_dev:
            unfixable.setdefault(by_dev[d], []).extend(probs)
    merged = []
    for g in groups:                       # union of overlapping sets
        hit = [m for m in merged if m & g]
        for m in hit:
            merged.remove(m)
            g = g | m
        merged.append(g)
    return [sorted(g) for g in merged], unfixable


# ---------- network fix-up (first boot, inside the guest) ----------

NETFIX_SH = r'''#!/bin/sh
# v2v-netfix — installed by the VMware->Exoscale receiver. Every boot, before
# networking: re-point interface configs written for VMware NIC names/MACs at
# the NICs that exist now. Idempotent; log /var/log/v2v-netfix.log.
R="${V2V_ROOT:-}"; SYS="${V2V_SYS:-/sys}"
LOG="$R/var/log/v2v-netfix.log"; mkdir -p "$R/var/log"
exec >>"$LOG" 2>&1
echo "== $(date -Is 2>/dev/null || date) v2v-netfix"
i=0
while [ $i -lt 30 ]; do
  for d in "$SYS"/class/net/*/device; do [ -e "$d" ] && break 2; done
  sleep 1; i=$((i+1))
done
NICS=$(for d in "$SYS"/class/net/*; do [ -e "$d/device" ] && basename "$d"; done | sort)
[ -n "$NICS" ] || { echo "no NICs found"; exit 0; }
echo "nics: $(echo $NICS)"
IFUP="$R/etc/network/interfaces $(ls "$R"/etc/network/interfaces.d/* 2>/dev/null)"
NETPLAN=$(ls "$R"/etc/netplan/*.yaml "$R"/etc/netplan/*.yml 2>/dev/null)
NMKEY=$(ls "$R"/etc/NetworkManager/system-connections/* 2>/dev/null)
IFCFG=$(ls "$R"/etc/sysconfig/network-scripts/ifcfg-* 2>/dev/null | grep -v 'ifcfg-lo$')
names() {
  { for f in $IFUP; do [ -f "$f" ] && awk '$1=="auto"||$1=="allow-hotplug"||$1=="iface"{for(i=2;i<=NF;i++){if($1=="iface"&&i>2)break;print $i}}' "$f"; done
    for f in $NETPLAN; do awk '/^[[:space:]]*ethernets:/{e=1;ind=-1;next} e&&/^[^[:space:]]/{e=0} e&&/^[[:space:]]+[A-Za-z0-9._-]+:[[:space:]]*$/{n=match($0,/[^ ]/); if(ind<0)ind=n; if(n==ind){sub(/^[[:space:]]+/,"");sub(/:.*/,"");print}}' "$f"; done
    for f in $NMKEY; do sed -n 's/^interface-name=//p' "$f"; done
    for f in $IFCFG; do sed -n 's/^DEVICE=["]*\([^"]*\)["]*$/\1/p' "$f"; done
  } | grep -v '^lo$' | grep -v ':' | sort -u
}
CONF=$(names)
STALE=$(for n in $CONF; do echo "$NICS" | grep -qx "$n" || echo "$n"; done)
FREE=$(for n in $NICS; do echo "$CONF" | grep -qx "$n" || echo "$n"; done)
echo "configured: $(echo $CONF) stale: $(echo $STALE) free: $(echo $FREE)"
set -- $FREE
for old in $STALE; do
  [ $# -gt 0 ] || { echo "no free NIC for $old"; break; }
  new=$1; shift
  echo "map $old -> $new"
  for f in $IFUP $NETPLAN $IFCFG; do [ -f "$f" ] && sed -i -E "s/(^|[^A-Za-z0-9._-])$old([^A-Za-z0-9._-]|$)/\1$new\2/g" "$f"; done
  for f in $NMKEY; do sed -i "s/^interface-name=$old\$/interface-name=$new/" "$f"; done
  [ -f "$R/etc/sysconfig/network-scripts/ifcfg-$old" ] && mv "$R/etc/sysconfig/network-scripts/ifcfg-$old" "$R/etc/sysconfig/network-scripts/ifcfg-$new"
done
# ifupdown: allow-hotplug events may have fired before this ran -> bring up via networking.service
for f in $IFUP; do [ -f "$f" ] && sed -i -E 's/^allow-hotplug([[:space:]]+)(e[a-z0-9]+)/auto\1\2/' "$f"; done
# drop VMware MAC pins (00:50:56 / 00:0c:29 / 00:05:69) that can never match again
VMOUI='(00:50:56|00:0[cC]:29|00:05:69)'
IFCFG=$(ls "$R"/etc/sysconfig/network-scripts/ifcfg-* 2>/dev/null | grep -v 'ifcfg-lo$')   # after renames
for f in $NMKEY; do sed -i -E "/^(mac-address|cloned-mac-address)=$VMOUI/Id" "$f"; done
for f in $IFCFG; do sed -i -E "/^(HWADDR|MACADDR)=[\"]?$VMOUI/Id" "$f"; done
for f in $NETPLAN; do
  sed -i -E "/macaddress:[[:space:]]*[\"']?$VMOUI/Id" "$f"
  # a `match:` left without children would be null -> drop it
  awk '{l[NR]=$0} END{for(i=1;i<=NR;i++){ if(l[i] ~ /^[[:space:]]*match:[[:space:]]*$/){ match(l[i],/[^ ]/); a=RSTART; match(l[i+1],/[^ ]/); b=RSTART; if(i==NR || b<=a) continue } print l[i] }}' "$f" > "$f.v2v" && cat "$f.v2v" > "$f" && rm -f "$f.v2v"
done
mkdir -p "$R/var/lib/v2v-netfix/disabled"
for f in "$R"/etc/udev/rules.d/*persistent-net*.rules "$R"/etc/systemd/network/*.link; do
  [ -f "$f" ] && grep -qiE "$VMOUI" "$f" && mv "$f" "$R/var/lib/v2v-netfix/disabled/" && echo "disabled $f"
done
[ -n "$NETPLAN" ] && [ -z "$R" ] && command -v netplan >/dev/null && netplan generate && echo "netplan regenerated"
# fallback: nothing configured for any NIC -> DHCP on the first one
CONF=$(names)
any=""; for n in $NICS; do echo "$CONF" | grep -qx "$n" && any=1; done
if [ -z "$any" ]; then
  first=$(echo "$NICS" | head -1)
  if [ -f "$R/etc/network/interfaces" ]; then
    mkdir -p "$R/etc/network/interfaces.d"
    printf 'auto %s\niface %s inet dhcp\n' "$first" "$first" > "$R/etc/network/interfaces.d/v2v-$first"
  elif [ -d "$R/etc/NetworkManager" ]; then
    : # NetworkManager auto-creates a DHCP profile for unmanaged ethernet
  elif [ -d "$R/etc/systemd/network" ]; then
    printf '[Match]\nName=%s\n[Network]\nDHCP=yes\n' "$first" > "$R/etc/systemd/network/90-v2v-fallback.network"
  fi
  echo "fallback DHCP on $first"
fi
exit 0
'''

NETFIX_UNIT = """[Unit]
Description=v2v: adapt network config to the NICs present (VMware -> Exoscale)
DefaultDependencies=no
After=local-fs.target systemd-udevd.service
Before=network-pre.target networking.service NetworkManager.service systemd-networkd.service network.service
Wants=network-pre.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/v2v-netfix
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""


def _guestfish(args, script, env):
    p = subprocess.run(["guestfish", *args], input=script, text=True, capture_output=True, env=env)
    return p.returncode, p.stdout, p.stderr


WIN_ONLINE_PS1 = r"""# exo-v2v: Windows Server keeps disks it has not seen before OFFLINE (SAN policy
# OfflineShared). After the move to virtio every data volume is "new": bring them
# online, make future Exoscale volumes come online too, then remove this task.
# Retries for up to 15 min: the storage services may not answer yet at early boot.
$log = "$env:ProgramData\exo-v2v\online-disks.log"
for ($i = 0; $i -lt 90; $i++) {
  try {
    Set-StorageSetting -NewDiskPolicy OnlineAll -ErrorAction Stop
    Get-Disk -ErrorAction Stop | Where-Object IsOffline | ForEach-Object {
      Set-Disk -Number $_.Number -IsOffline $false -ErrorAction Stop
      Set-Disk -Number $_.Number -IsReadOnly $false -ErrorAction Stop
      "$(Get-Date -Format o) disk $($_.Number) online" | Add-Content $log
    }
    if (-not (Get-Disk | Where-Object IsOffline) -and (Get-StorageSetting).NewDiskPolicy -eq "OnlineAll") {
      "$(Get-Date -Format o) done" | Add-Content $log
      schtasks /delete /tn exo-v2v-online-disks /f | Out-Null
      exit 0
    }
  } catch { "$(Get-Date -Format o) $($_.Exception.Message)" | Add-Content $log }
  Start-Sleep -Seconds 10
}
exit 1
"""
WIN_ONLINE_BAT = r"""@echo off
rem exo-v2v: register a SYSTEM task that brings the data disks online (it retries
rem until the storage services answer, runs at every boot until it has succeeded,
rem then deletes itself), and start it now. Doing the work here directly failed on
rem Windows Server 2025: the firstboot window ends before the storage stack is up.
rem virt-v2v's firstboot may also run this while Windows is already shutting down
rem for a driver reboot (schtasks then fails): queue this script again first, so
rem the next boot retries it, and drop that copy once the task exists.
set Q="C:\Program Files\Guestfs\Firstboot\scripts\9000-exo-v2v-online-disks.bat"
copy /y "%~f0" %Q% >nul
for /l %%i in (1,1,12) do (
  schtasks /create /tn exo-v2v-online-disks /ru SYSTEM /rl HIGHEST /sc ONSTART /f /tr "powershell -NoProfile -ExecutionPolicy Bypass -File C:\ProgramData\exo-v2v\online-disks.ps1" && goto created
  ping -n 11 127.0.0.1 >nul
)
exit /b 1
:created
del /f %Q% >nul 2>&1
schtasks /run /tn exo-v2v-online-disks
exit /b 0
"""
WIN_FIRSTBOOT = "/Program Files/Guestfs/Firstboot/scripts"


def winfix(root_disk, root, env):
    """Windows: queue a first-boot script in virt-v2v's own firstboot framework
    (rhsrvany runs scripts/*.bat in order) that registers the online-disks task."""
    with tempfile.TemporaryDirectory() as td:
        bat, ps1 = os.path.join(td, "online.bat"), os.path.join(td, "online.ps1")
        open(bat, "w", newline="\r\n").write(WIN_ONLINE_BAT)
        open(ps1, "w", newline="\r\n").write(WIN_ONLINE_PS1)
        script = (f"run\nmount {root} /\n"
                  f"is-dir \"{WIN_FIRSTBOOT}\"\n")
        rc, out, err = _guestfish(["-a", root_disk], script, env)
        if rc or out.strip() != "true":
            return {"applied": False, "reason": "virt-v2v firstboot framework not found"}
        rc, out, err = _guestfish(["-a", root_disk],
                                  f"run\nmount {root} /\nmkdir-p /ProgramData/exo-v2v\n"
                                  f"upload {ps1} /ProgramData/exo-v2v/online-disks.ps1\n"
                                  f"upload {bat} \"{WIN_FIRSTBOOT}/9000-exo-v2v-online-disks.bat\"\n", env)
        if rc:
            return {"applied": False, "reason": err.strip()[-300:]}
    return {"applied": True, "root": root, "distro": "windows", "fix": "online disks (SAN policy OnlineAll)"}


def netfix(root_disk):
    """Inject v2v-netfix into a Linux guest (3 appliance launches: inspect,
    probe, write+label). Non-Linux guests are left alone."""
    env = dict(os.environ, LIBGUESTFS_BACKEND="direct")
    rc, out, err = _guestfish(["--ro", "-a", root_disk], "run\ninspect-os\n", env)
    roots = [r for r in out.split() if r.startswith("/dev/")]
    if rc or not roots:
        return {"applied": False, "reason": f"inspect-os failed: {err.strip()[-300:]}"}
    root = roots[0]
    probe = (f"run\ninspect-os\ninspect-get-type {root}\ninspect-get-distro {root}\n"
             f"mount-ro {root} /\nis-file /etc/selinux/config\n")
    rc, out, err = _guestfish(["--ro", "-a", root_disk], probe, env)
    tail3 = out.split()[-3:]
    if rc or len(tail3) < 3:
        return {"applied": False, "reason": f"probe failed: {err.strip()[-300:]}"}
    kind, distro, selinux = tail3[0], tail3[1], tail3[2] == "true"
    if kind == "windows":
        return winfix(root_disk, root, env)
    if kind != "linux":
        return {"applied": False, "reason": f"guest type {kind}: not handled"}
    files = [("/usr/local/sbin/v2v-netfix", NETFIX_SH, "0755", "bin_t"),
             ("/etc/systemd/system/v2v-netfix.service", NETFIX_UNIT, "0644", "systemd_unit_file_t")]
    wants = "/etc/systemd/system/multi-user.target.wants/v2v-netfix.service"
    with tempfile.TemporaryDirectory() as td:
        lines = ["mkdir-p /usr/local/sbin", "mkdir-p /etc/systemd/system/multi-user.target.wants"]
        for i, (path, body, mode, _t) in enumerate(files):
            local = os.path.join(td, f"f{i}")
            open(local, "w").write(body)
            lines += [f"upload {local} {path}", f"chmod {mode} {path}"]
        lines.append(f"ln-sf ../v2v-netfix.service {wants}")
        if selinux:  # label just these files: no full relabel / extra reboot
            for path, _b, _m, t in files:
                ctx = f"system_u:object_r:{t}:s0"
                lines.append(f'setxattr security.selinux "{ctx}" {len(ctx) + 1} {path}')
            ctx = "system_u:object_r:systemd_unit_file_t:s0"
            lines.append(f'lsetxattr security.selinux "{ctx}" {len(ctx) + 1} {wants}')
        # -i mounts the guest's own fstab (separate /usr, /etc on LVM, ...)
        rc, out, err = _guestfish(["-a", root_disk, "-i"], "\n".join(lines) + "\n", env)
        if rc:
            return {"applied": False, "reason": err.strip()[-300:]}
    return {"applied": True, "root": root, "distro": distro, "selinux_labelled": selinux}


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("analyze", "adapt", "netfix", "relayout"):
        sys.exit(__doc__)
    if sys.argv[1] == "relayout":
        print(json.dumps(relayout(sys.argv[2], sys.argv[3]), indent=1))
        return
    cmd, devs = sys.argv[1], sys.argv[2:]
    fn = {"analyze": analyze, "adapt": adapt, "netfix": netfix}[cmd]
    print(json.dumps([fn(d) for d in devs], indent=1))


if __name__ == "__main__":
    main()
