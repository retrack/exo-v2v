#!/usr/bin/env python3
"""Boot mask: the receiver keeps the first and the last MiB of the replicated
system disk (MBR or GPT, boot loader, backup GPT) off the disk until the
conversion. Writes there go to side files in RAM (MASK_DIR), reads return them,
and the disk itself holds zeros: an instance restarted in the middle of the
replication finds nothing to boot, instead of booting an unconverted copy of
the source VM on the network (which would also write to its volumes behind the
change tracking). The conversion writes the side files to the disk first
(unmask), once.

mask.json, next to the agent (written by exo-v2v): {"sda": [[offset, length], ...]}
"""
import json
import os

MASK_DIR = os.environ.get("V2V_MASK_DIR", "/run/exo-v2v/mask")
UNMASKED = "unmasked"        # flag file: the side files are on the disk, masking is over


def config(path):
    """{export: [(offset, length)]} from mask.json ({} without one)."""
    try:
        with open(path) as f:
            return {k: [(int(o), int(n)) for o, n in v] for k, v in json.load(f).items()}
    except FileNotFoundError:
        return {}


def active(mask_dir=MASK_DIR):
    return not os.path.exists(os.path.join(mask_dir, UNMASKED))


def side_path(mask_dir, export, offset):
    return os.path.join(mask_dir, f"{export}-{offset}.bin")


def _pread_all(fd, n, off):
    buf = bytearray()
    while len(buf) < n:
        b = os.pread(fd, n - len(buf), off + len(buf))
        if not b:
            break
        buf += b
    return bytes(buf)


def _zero(fd, off, n):
    step = 1 << 20
    for o in range(off, off + n, step):
        os.pwrite(fd, bytes(min(step, off + n - o)), o)


class Mask:
    """The masked ranges of one export, opened on its device. create=True (the
    NBD server) sets up missing side files from the device's current content -
    a receiver upgraded in the middle of a replication keeps its replica's boot
    sectors - and zeroes those ranges on the device."""

    def __init__(self, export, ranges, dev_fd, mask_dir=MASK_DIR, create=True):
        size = os.lseek(dev_fd, 0, os.SEEK_END)
        self.regions = []                         # [(offset, length, side fd)], sorted
        if create:
            os.makedirs(mask_dir, mode=0o700, exist_ok=True)
        for off, n in sorted(ranges):
            n = min(n, size - off)
            if n <= 0:
                continue
            path = side_path(mask_dir, export, off)
            if create and not os.path.exists(path):
                tmp = path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(_pread_all(dev_fd, n, off).ljust(n, b"\0"))
                    f.flush()
                    os.fsync(f.fileno())
                os.rename(tmp, path)
            if create:
                _zero(dev_fd, off, n)             # again after a restart: idempotent
            self.regions.append((off, n, os.open(path, os.O_RDWR if create else os.O_RDONLY)))
        if create:
            os.fsync(dev_fd)

    def segments(self, offset, length):
        """[(start, end, side fd or None)] covering [offset, offset+length)."""
        out, pos, end = [], offset, offset + length
        for off, n, fd in self.regions:
            if off + n <= pos or off >= end:
                continue
            if off > pos:
                out.append((pos, off, None))
            s, e = max(pos, off), min(end, off + n)
            out.append((s, e, fd))
            pos = e
        if pos < end:
            out.append((pos, end, None))
        return out

    def _base(self, fd):
        return next(off for off, _n, sfd in self.regions if sfd == fd)

    def read(self, dev_fd, offset, length):
        buf = bytearray()
        for s, e, fd in self.segments(offset, length):
            b = _pread_all(dev_fd, e - s, s) if fd is None else _pread_all(fd, e - s, s - self._base(fd))
            buf += b
            if len(b) < e - s:
                break
        return bytes(buf)

    def write(self, dev_fd, data, offset):
        for s, e, fd in self.segments(offset, len(data)):
            chunk = data[s - offset:e - offset]
            if fd is None:
                os.pwrite(dev_fd, chunk, s)
            else:
                os.pwrite(fd, chunk, s - self._base(fd))

    def zero(self, dev_fd, offset, length, zero_range):
        """zero_range(offset, length) zeroes unmasked parts on the device."""
        for s, e, fd in self.segments(offset, length):
            if fd is None:
                zero_range(s, e - s)
            else:
                _zero(fd, s - self._base(fd), e - s)

    def fsync(self):
        for _off, _n, fd in self.regions:
            os.fsync(fd)

    def close(self):
        for _off, _n, fd in self.regions:
            os.close(fd)
        self.regions = []


def reader(export, ranges, dev_fd, mask_dir=MASK_DIR):
    """A read-only overlay (rootcopy, /hashmap) or None when nothing is masked."""
    if not ranges or not active(mask_dir) or \
            not all(os.path.exists(side_path(mask_dir, export, off)) for off, _n in ranges):
        return None
    return Mask(export, ranges, dev_fd, mask_dir, create=False)


def unmask(exports, cfg, mask_dir=MASK_DIR):
    """Write the side files to the disks, then set the flag (once: a conversion
    that runs again must not undo what the first one wrote). Returns what it did."""
    if not active(mask_dir):
        return "already unmasked"
    done = []
    for export, ranges in cfg.items():
        dev = exports.get(export)
        if not dev:
            continue
        fd = os.open(dev, os.O_RDWR)
        try:
            for off, n in ranges:
                path = side_path(mask_dir, export, off)
                if not os.path.exists(path):
                    continue              # never masked (the NBD server was not started with it)
                with open(path, "rb") as f:
                    data = f.read()
                os.pwrite(fd, data, off)
                done.append(f"{export}@{off}+{len(data)}")
            os.fsync(fd)
        finally:
            os.close(fd)
    os.makedirs(mask_dir, mode=0o700, exist_ok=True)
    with open(os.path.join(mask_dir, UNMASKED), "w") as f:
        f.write(json.dumps(done))
        f.flush()
        os.fsync(f.fileno())
    return done
