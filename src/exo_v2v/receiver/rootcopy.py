#!/usr/bin/env python3
"""rootcopy.py SRC HOST:PORT EXPORT [WORKERS] — copy a block device onto an NBD
export: 8 MiB chunks, zero chunks sent as WRITE_ZEROES, WORKERS connections in
parallel (default 4). Prints a JSON summary.

exo-v2v rehearsal: runs on a production receiver (its replica is not written
while the migration's lock is held) and fills the system disk of the rehearsal
copy's receiver, reached through the relay."""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nbdproto as P  # noqa: E402

CHUNK = 8 << 20


def main():
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    src, addr, export = sys.argv[1:4]
    workers = int(sys.argv[4]) if len(sys.argv) > 4 else 4
    host, port = addr.rsplit(":", 1)
    fd = os.open(src, os.O_RDONLY)
    size = os.lseek(fd, 0, os.SEEK_END)
    os.close(fd)
    probe = P.Client(host, int(port), export)
    if probe.size < size:
        sys.exit(f"{export}: target {probe.size} B < source {size} B")
    probe.close()
    pos, lock, stats, errors = [0], threading.Lock(), {"data": 0, "zeros": 0}, []
    t0 = time.time()

    def worker():
        try:
            f = os.open(src, os.O_RDONLY)
            c = P.Client(host, int(port), export, window=8)
            while True:
                with lock:
                    off = pos[0]
                    pos[0] += CHUNK
                if off >= size:
                    break
                n = min(CHUNK, size - off)
                buf = os.pread(f, n, off)
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
            os.close(f)
        except Exception as e:          # reported below, the copy is then incomplete
            errors.append(repr(e))

    th = [threading.Thread(target=worker) for _ in range(workers)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    dt = time.time() - t0
    out = {"bytes": size, **stats, "seconds": round(dt, 1),
           "mib_s": round(size / dt / 2**20, 1) if dt else 0, "errors": errors}
    print(json.dumps(out))
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
