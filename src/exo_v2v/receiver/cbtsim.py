#!/usr/bin/env python3
"""CBT simulator and disk comparison helpers (no VMware needed).

Changed-extent files use the same shape as VMware QueryChangedDiskAreas:
a JSON list of {"start": offset, "length": bytes}, sorted and merged.

  cbtsim.py make    SRC.img --size 2G [--fill 0.3]      # sparse disk, 30% random data
  cbtsim.py mutate  SRC.img --writes 200 -o round1.json # random writes, emits extents
  cbtsim.py diff    OLD.img NEW.img -o delta.json       # extents where two images differ
  cbtsim.py hashmap DISK [--chunk 64M] -o map.json      # per-chunk sha256 (file or /dev/*)
  cbtsim.py compare a.json b.json                       # exit 0 if identical

`diff` turns any two versions of a real OS image (e.g. before/after
`virt-customize --install`) into a realistic, bootable delta for testing.
"""
import argparse
import hashlib
import json
import os
import random
import sys

GRAIN = 64 * 1024  # CBT reports at >= 64 KiB granularity on VMFS in practice


def parse_size(s):
    s = str(s).strip().upper()
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
    if s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(s)


def merge(extents):
    out = []
    for e in sorted(extents, key=lambda x: x["start"]):
        if out and e["start"] <= out[-1]["start"] + out[-1]["length"]:
            end = max(out[-1]["start"] + out[-1]["length"], e["start"] + e["length"])
            out[-1]["length"] = end - out[-1]["start"]
        else:
            out.append(dict(e))
    return out


def disk_size(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


def write_json(obj, path):
    if path in (None, "-"):
        json.dump(obj, sys.stdout, indent=1)
        print()
    else:
        with open(path, "w") as f:
            json.dump(obj, f, indent=1)


def cmd_make(a):
    size = parse_size(a.size)
    rng = random.Random(a.seed)
    with open(a.image, "wb") as f:
        f.truncate(size)
        nblocks = int(size * a.fill) // (1 << 20)
        for _ in range(nblocks):
            off = rng.randrange(0, size // (1 << 20)) * (1 << 20)
            f.seek(off)
            f.write(rng.randbytes(1 << 20))
    print(f"created {a.image}: {size} bytes, ~{a.fill:.0%} data")


def cmd_mutate(a):
    size = disk_size(a.image)
    rng = random.Random(a.seed)
    extents = []
    with open(a.image, "r+b") as f:
        for _ in range(a.writes):
            length = rng.randint(1, a.max_grains) * GRAIN
            start = rng.randrange(0, (size - length) // GRAIN + 1) * GRAIN
            f.seek(start)
            f.write(rng.randbytes(length))
            extents.append({"start": start, "length": length})
    extents = merge(extents)
    write_json(extents, a.output)
    total = sum(e["length"] for e in extents)
    print(f"{len(extents)} extents, {total} bytes changed ({total / size:.2%})", file=sys.stderr)


def cmd_diff(a):
    if disk_size(a.old) != disk_size(a.new):
        sys.exit("images differ in size")
    extents = []
    with open(a.old, "rb") as fo, open(a.new, "rb") as fn:
        off = 0
        while True:
            bo, bn = fo.read(GRAIN), fn.read(GRAIN)
            if not bn:
                break
            if bo != bn:
                extents.append({"start": off, "length": len(bn)})
            off += len(bn)
    extents = merge(extents)
    write_json(extents, a.output)
    print(f"{len(extents)} extents, {sum(e['length'] for e in extents)} bytes differ", file=sys.stderr)


def hashmap(path, chunk):
    size = disk_size(path)
    hashes = []
    with open(path, "rb", buffering=0) as f:
        for off in range(0, size, chunk):
            h = hashlib.sha256()
            left = min(chunk, size - off)
            while left:
                b = f.read(min(left, 8 << 20))
                if not b:
                    break
                h.update(b)
                left -= len(b)
            hashes.append(h.hexdigest())
    return {"size": size, "chunk": chunk, "sha256": hashes}


def cmd_hashmap(a):
    write_json(hashmap(a.disk, parse_size(a.chunk)), a.output)


def cmd_compare(a):
    x, y = (json.load(open(p)) for p in (a.a, a.b))
    if x["size"] != y["size"] or x["chunk"] != y["chunk"]:
        sys.exit(f"MISMATCH: size/chunk differ ({x['size']}/{x['chunk']} vs {y['size']}/{y['chunk']})")
    bad = [i for i, (h1, h2) in enumerate(zip(x["sha256"], y["sha256"])) if h1 != h2]
    if bad:
        print(f"MISMATCH in {len(bad)} chunk(s), first at offset {bad[0] * x['chunk']}")
        sys.exit(1)
    print(f"IDENTICAL: {x['size']} bytes, {len(x['sha256'])} chunks")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("make"); p.add_argument("image"); p.add_argument("--size", default="1G")
    p.add_argument("--fill", type=float, default=0.3); p.add_argument("--seed", type=int, default=1)
    p.set_defaults(fn=cmd_make)
    p = sub.add_parser("mutate"); p.add_argument("image"); p.add_argument("--writes", type=int, default=100)
    p.add_argument("--max-grains", type=int, default=32); p.add_argument("--seed", type=int)
    p.add_argument("-o", "--output"); p.set_defaults(fn=cmd_mutate)
    p = sub.add_parser("diff"); p.add_argument("old"); p.add_argument("new"); p.add_argument("-o", "--output")
    p.set_defaults(fn=cmd_diff)
    p = sub.add_parser("hashmap"); p.add_argument("disk"); p.add_argument("--chunk", default="64M")
    p.add_argument("-o", "--output"); p.set_defaults(fn=cmd_hashmap)
    p = sub.add_parser("compare"); p.add_argument("a"); p.add_argument("b"); p.set_defaults(fn=cmd_compare)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
