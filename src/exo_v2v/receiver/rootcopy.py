#!/usr/bin/env python3
"""rootcopy.py SRC HOST:PORT EXPORT [WORKERS] [--overlay NAME [--mask-config FILE]]
— copy a block device onto an NBD export: 8 MiB chunks, zero chunks sent as
WRITE_ZEROES, WORKERS connections in parallel (default 4). Prints a JSON summary.

exo-v2v rehearsal: runs on a production receiver (its replica is not written
while the migration's lock is held) and fills the system disk of the rehearsal
copy's receiver, reached through the relay. --overlay NAME reads SRC through
that receiver's boot mask for export NAME (bootmask.py; ranges from the agent's
mask.json), so the copy gets the real boot sectors."""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bootmask  # noqa: E402
import nbdproto as P  # noqa: E402

CHUNK = 8 << 20


def option(args, name, default=None):
    if name in args:
        i = args.index(name)
        val = args[i + 1]
        del args[i:i + 2]
        return val
    return default


def main():
    args = sys.argv[1:]
    overlay = option(args, "--overlay")
    mask_config = option(args, "--mask-config", "/opt/v2v-agent/mask.json")
    if len(args) < 3:
        sys.exit(__doc__)
    src, addr, export = args[:3]
    workers = int(args[3]) if len(args) > 3 else 4
    host, port = addr.rsplit(":", 1)
    fd = os.open(src, os.O_RDONLY)
    size = os.lseek(fd, 0, os.SEEK_END)
    os.close(fd)
    probe = P.Client(host, int(port), export)
    if probe.size < size:
        sys.exit(f"{export}: target {probe.size} B < source {size} B")
    probe.close()
    ranges = bootmask.config(mask_config).get(overlay) if overlay else None
    pos, lock, stats, errors = [0], threading.Lock(), {"data": 0, "zeros": 0}, []
    masked = []
    t0 = time.time()

    def worker():
        try:
            f = os.open(src, os.O_RDONLY)
            ov = bootmask.reader(overlay, ranges, f) if ranges else None
            if ov:
                masked.append(1)
            c = P.Client(host, int(port), export, window=8)
            while True:
                with lock:
                    off = pos[0]
                    pos[0] += CHUNK
                if off >= size:
                    break
                n = min(CHUNK, size - off)
                buf = ov.read(f, off, n) if ov else os.pread(f, n, off)
                if buf.count(0) == n:
                    c.write_zeroes(off, n)
                    k = "zeros"
                else:
                    c.write(off, buf)
                    k = "data"
                with lock:
                    stats[k] += n
            c.flush()
            c.close()
            if ov:
                ov.close()
            os.close(f)
        except Exception as e:          # reported below, the copy is then incomplete
            errors.append(repr(e))

    th = [threading.Thread(target=worker) for _ in range(workers)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    dt = time.time() - t0
    out = {"bytes": size, **stats, "overlay": bool(masked), "seconds": round(dt, 1),
           "mib_s": round(size / dt / 2**20, 1) if dt else 0, "errors": errors}
    print(json.dumps(out))
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
