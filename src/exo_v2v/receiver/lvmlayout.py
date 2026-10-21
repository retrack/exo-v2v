#!/usr/bin/env python3
"""LVM2 on-disk layout, read without the LVM tools (the receiver must not
activate the guest's volume groups: they sit on disks it is writing).

- PV label (LABELONE, one of the first four 512-byte sectors), PV header (UUID,
  data and metadata areas), metadata area header and the current VG metadata
  text (circular buffer) - lvm2 lib/format_text/layout.h, lib/label/label.h.
- The text metadata is parsed into dicts; logical volumes are mapped to byte
  ranges on the devices that hold their physical volumes.
- dm tables (512-byte units) to assemble a logical volume from those ranges,
  e.g. over read-only 512-sector loop views of 4Kn volumes.
"""
import re
import struct
import subprocess

SECTOR = 512
MDA_MAGIC = b"\x20LVM2\x20x[5A%r0N*>"
LV_TYPES_REBUILDABLE = ("striped",)      # linear = striped with stripe_count 1


def _pread(dev, off, n):
    with open(dev, "rb", buffering=0) as f:
        f.seek(off)
        return f.read(n)


# ---------- text metadata ----------

_TOK = re.compile(r'\s*(?:(#[^\n]*)|("(?:[^"\\]|\\.)*")|(-?\d+)|([A-Za-z0-9_.+\-]+)|(.))', re.S)


def parse_text(text):
    """LVM text metadata -> nested dict (sections are dicts, arrays lists)."""
    toks = []
    for m in _TOK.finditer(text):
        if m.group(1):
            continue
        if m.group(2) is not None:
            toks.append(("s", m.group(2)[1:-1].replace('\\"', '"').replace("\\\\", "\\")))
        elif m.group(3) is not None:
            toks.append(("n", int(m.group(3))))
        elif m.group(4) is not None:
            toks.append(("i", m.group(4)))
        elif m.group(5) and not m.group(5).isspace() and m.group(5) != "\x00":
            toks.append(("p", m.group(5)))
    pos = 0

    def value():
        nonlocal pos
        k, v = toks[pos]
        if k == "p" and v == "[":
            pos += 1
            out = []
            while toks[pos] != ("p", "]"):
                if toks[pos] == ("p", ","):
                    pos += 1
                    continue
                out.append(toks[pos][1])
                pos += 1
            pos += 1
            return out
        pos += 1
        return v

    def section(end):
        nonlocal pos
        d = {}
        while pos < len(toks) and toks[pos] != end:
            k, name = toks[pos]
            pos += 1
            if toks[pos] == ("p", "{"):
                pos += 1
                d[name] = section(("p", "}"))
                pos += 1
            elif toks[pos] == ("p", "="):
                pos += 1
                d[name] = value()
            else:
                raise ValueError(f"LVM metadata: unexpected token after {name!r}: {toks[pos]}")
        return d

    return section(None)


# ---------- label, PV header, metadata area ----------

def read_pv(dev, off=0):
    """PV at byte offset `off` of `dev` -> {uuid, dev_size, data_areas, mdas, text, vg} or None."""
    head = _pread(dev, off, 4 * SECTOR)
    for s in range(4):
        lab = head[s * SECTOR:(s + 1) * SECTOR]
        if lab[:8] == b"LABELONE" and lab[24:32] == b"LVM2 001":
            break
    else:
        return None
    (offset_xl,) = struct.unpack_from("<I", lab, 20)
    ph = lab[offset_xl:]
    raw_uuid = ph[:32].decode(errors="replace")
    (dev_size,) = struct.unpack_from("<Q", ph, 32)
    pos, lists = 40, []
    for _ in range(2):                    # data areas, then metadata areas
        cur = []
        while pos + 16 <= len(ph):
            o, n = struct.unpack_from("<QQ", ph, pos)
            pos += 16
            if o == 0 and n == 0:
                break
            cur.append((o, n))
        lists.append(cur)
    pv = {"uuid": dashed(raw_uuid), "dev_size": dev_size, "data_areas": lists[0], "mdas": lists[1],
          "label_sector": s, "text": None, "vg": None}
    for mo, msz in lists[1]:
        text = read_mda_text(dev, off + mo, msz)
        if text:
            pv["text"] = text
            break
    if pv["text"]:
        doc = parse_text(pv["text"])
        vgs = [(k, v) for k, v in doc.items() if isinstance(v, dict) and "physical_volumes" in v]
        if vgs:
            pv["vg"] = {"name": vgs[0][0], **vgs[0][1]}
    return pv


def read_mda_text(dev, mda_off, mda_size):
    hdr = _pread(dev, mda_off, SECTOR)
    if hdr[4:20] != MDA_MAGIC:
        return None
    o, n, _ck, flags = struct.unpack_from("<QQII", hdr, 40)    # first raw_locn = current metadata
    if n == 0 or flags & 1:                                    # RAW_LOCN_IGNORED
        return None
    if o + n <= mda_size:
        data = _pread(dev, mda_off + o, n)
    else:                                                      # wrapped around the circular buffer
        first = mda_size - o
        data = _pread(dev, mda_off + o, first) + _pread(dev, mda_off + SECTOR, n - first)
    return data.split(b"\x00", 1)[0].decode(errors="replace")


def dashed(u):
    """LVM prints a 32-char UUID as 6-4-4-4-4-4-6."""
    u = u.replace("-", "")
    parts, i = [], 0
    for n in (6, 4, 4, 4, 4, 4, 6):
        parts.append(u[i:i + n])
        i += n
    return "-".join(parts)


# ---------- volume groups across devices ----------

def scan(places):
    """places: [(dev, byte offset, size)] that may hold a PV (partitions and whole
    disks). Returns {vg_id: vg} with, per PV, where it was found and, per LV, its
    byte extents [(dev, offset, length)] in LV order (None for non-linear types)."""
    found = {}
    for dev, off, size in places:
        pv = read_pv(dev, off)
        if pv and pv["vg"]:
            found[pv["uuid"]] = (dev, off, size, pv)
    vgs = {}
    for uuid, (_dev, _off, _size, pv) in found.items():
        vg = pv["vg"]
        cur = vgs.get(vg["id"])
        if cur and cur["seqno"] >= vg.get("seqno", 0):
            continue
        vgs[vg["id"]] = {"name": vg["name"], "id": vg["id"], "seqno": vg.get("seqno", 0),
                         "extent_bytes": vg["extent_size"] * SECTOR, "meta": vg}
    for vgid, v in vgs.items():
        meta, eb = v["meta"], v["extent_bytes"]
        pvs = {}
        for pname, p in meta["physical_volumes"].items():
            loc = found.get(p["id"])
            pvs[pname] = {"id": p["id"], "pe_start": p["pe_start"] * SECTOR, "pe_count": p["pe_count"],
                          "dev": loc[0] if loc else None, "offset": loc[1] if loc else None,
                          "size": loc[2] if loc else None, "mdas": loc[3]["mdas"] if loc else []}
        lvs = {}
        for lname, lv in (meta.get("logical_volumes") or {}).items():
            segs, linear, types = [], True, set()
            for sk in sorted((k for k in lv if k.startswith("segment") and isinstance(lv[k], dict)),
                             key=lambda k: lv[k]["start_extent"]):
                sg = lv[sk]
                types.add(sg["type"])
                if sg["type"] != "striped":
                    linear = False
                    continue
                n = sg.get("stripe_count", 1)
                stripes = sg["stripes"]
                legs = [(stripes[2 * i], stripes[2 * i + 1]) for i in range(n)]
                segs.append({"start": sg["start_extent"] * eb, "length": sg["extent_count"] * eb,
                             "stripe_bytes": sg.get("stripe_size", 0) * SECTOR, "legs": [
                                 {"pv": pn, "dev": pvs[pn]["dev"],
                                  "offset": None if pvs[pn]["dev"] is None else
                                  pvs[pn]["offset"] + pvs[pn]["pe_start"] + pe * eb}
                                 for pn, pe in legs]})
            lvs[lname] = {"id": lv.get("id"), "status": lv.get("status", []), "types": sorted(types),
                          "segments": segs if linear else None,
                          "devs": sorted({leg["dev"] for s in segs for leg in s["legs"] if leg["dev"]}),
                          "complete": linear and all(leg["dev"] for s in segs for leg in s["legs"])}
        v.update(pvs=pvs, lvs=lvs, complete=all(p["dev"] for p in pvs.values()))
        del v["meta"]
    return vgs


def lv_start(lv):
    """(dev, byte offset) of the LV's first byte (for filesystem probes)."""
    s = lv["segments"][0] if lv["segments"] else None
    if not s or not s["legs"][0]["dev"]:
        return None, None
    return s["legs"][0]["dev"], s["legs"][0]["offset"]


def dm_table(lv, dev_map=None):
    """dm table lines (512-byte sectors) assembling the LV; dev_map maps each
    PV device to the device to use instead (a loop view or a spare volume)."""
    lines = []
    for s in lv["segments"]:
        start, length = s["start"] // SECTOR, s["length"] // SECTOR
        legs = [((dev_map or {}).get(l["dev"], l["dev"]), l["offset"] // SECTOR) for l in s["legs"]]
        if len(legs) == 1:
            lines.append(f"{start} {length} linear {legs[0][0]} {legs[0][1]}")
        else:
            chunk = s["stripe_bytes"] // SECTOR
            lines.append(f"{start} {length} striped {len(legs)} {chunk} " + " ".join(f"{d} {o}" for d, o in legs))
    return "\n".join(lines) + "\n"


def lv_size(lv):
    return sum(s["length"] for s in lv["segments"])


def dm_create(name, table, readonly=False):
    # --noudevsync + mknodes: works with or without the dm udev rules installed
    cmd = ["dmsetup", "create", name, "--noudevsync"] + (["--readonly"] if readonly else [])
    r = subprocess.run(cmd, input=table, text=True, capture_output=True)
    if r.returncode:
        raise RuntimeError(f"dmsetup create {name}: {r.stderr.strip()[-300:]}")
    subprocess.call(["udevadm", "settle"])
    subprocess.call(["dmsetup", "mknodes", name])
    return f"/dev/mapper/{name}"


def dm_remove(name):
    for _ in range(10):
        if subprocess.call(["dmsetup", "remove", "--noudevsync", name], stderr=subprocess.DEVNULL) == 0:
            return
        subprocess.call(["udevadm", "settle"])
