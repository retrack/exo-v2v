#!/usr/bin/env python3
"""Minimal NBD server exporting block devices or files by name.

Runs on the receiver (agent in RAM): exports /dev/vda and the attached data
volumes so the on-prem sender can write the full copy and CBT deltas. Binds to
127.0.0.1 by default; the sender reaches it through an SSH tunnel.

  nbdserve.py --export sda=/dev/vda --export sdb=/dev/vdb [--bind 127.0.0.1] [--port 10809]
              [--mask sda=0:1048576 ...]      # boot mask (bootmask.py): kept off the disk

Interoperates with qemu-nbd/nbdcopy/nbd-client (simple replies only).
"""
import argparse
import ctypes
import fcntl
import logging
import os
import stat
import socket
import struct
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bootmask  # noqa: E402
import nbdproto as P  # noqa: E402

log = logging.getLogger("nbdserve")
ZERO_CHUNK = bytes(4 * 1024 * 1024)
BLKZEROOUT = 0x127F                      # _IO(0x12, 127)
FALLOC_PUNCH_HOLE_KEEP_SIZE = 0x02 | 0x01
_libc = ctypes.CDLL(None, use_errno=True)
_libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong]


def zero_range(fd, is_blk, offset, length):
    """Zero [offset, offset+length) cheaply: BLKZEROOUT on block devices (the
    kernel can offload it as WRITE_ZEROES/discard), hole-punch on files; plain
    zero writes only as a fallback."""
    try:
        if is_blk:
            fcntl.ioctl(fd, BLKZEROOUT, struct.pack("QQ", offset, length))
            return
        if _libc.fallocate(fd, FALLOC_PUNCH_HOLE_KEEP_SIZE, offset, length) == 0:
            return
    except OSError:
        pass
    o, end = offset, offset + length
    while o < end:
        n = min(len(ZERO_CHUNK), end - o)
        os.pwrite(fd, ZERO_CHUNK[:n], o)
        o += n
TFLAGS = (P.TF_HAS_FLAGS | P.TF_SEND_FLUSH | P.TF_SEND_FUA | P.TF_SEND_TRIM
          | P.TF_SEND_WRITE_ZEROES)


class Export:
    def __init__(self, name, path, readonly=False, mask=None):
        self.name, self.path = name, path
        self.fd = os.open(path, os.O_RDONLY if readonly else os.O_RDWR)
        self.size = os.lseek(self.fd, 0, os.SEEK_END)
        self.is_blk = stat.S_ISBLK(os.fstat(self.fd).st_mode)
        self.readonly = readonly
        self.lock = threading.Lock()
        self.bytes_written = 0
        # [(offset, length)]: ranges kept in side files until the conversion (bootmask.py)
        self.mask = bootmask.Mask(name, mask, self.fd) if mask and not readonly else None

    def tflags(self):
        return TFLAGS | (P.TF_READ_ONLY if self.readonly else 0)

    def pread(self, length, offset):
        if self.mask:
            return self.mask.read(self.fd, offset, length)
        buf = bytearray()
        while len(buf) < length:
            b = os.pread(self.fd, length - len(buf), offset + len(buf))
            if not b:
                break
            buf += b
        return bytes(buf)

    def pwrite(self, data, offset):
        if self.mask:
            self.mask.write(self.fd, data, offset)
        else:
            os.pwrite(self.fd, data, offset)

    def zero(self, offset, length):
        if self.mask:
            self.mask.zero(self.fd, offset, length, lambda o, n: zero_range(self.fd, self.is_blk, o, n))
        else:
            zero_range(self.fd, self.is_blk, offset, length)

    def fsync(self):
        os.fsync(self.fd)
        if self.mask:
            self.mask.fsync()


def send_opt_reply(sock, opt, rtype, data=b""):
    sock.sendall(struct.pack(">QIII", P.REPLY_MAGIC, opt, rtype, len(data)) + data)


def negotiate(sock, exports):
    sock.sendall(struct.pack(">QQH", P.NBDMAGIC, P.IHAVEOPT,
                             P.FLAG_FIXED_NEWSTYLE | P.FLAG_NO_ZEROES))
    (cflags,) = struct.unpack(">I", P.recv_exact(sock, 4))
    no_zeroes = bool(cflags & P.FLAG_NO_ZEROES)
    while True:
        magic, opt, length = struct.unpack(">QII", P.recv_exact(sock, 16))
        if magic != P.IHAVEOPT:
            raise ConnectionError("bad option magic")
        data = P.recv_exact(sock, length) if length else b""
        if opt == P.OPT_EXPORT_NAME:
            exp = exports.get(data.decode(errors="replace"))
            if exp is None:
                raise ConnectionError(f"unknown export {data!r}")
            sock.sendall(struct.pack(">QH", exp.size, exp.tflags())
                         + (b"" if no_zeroes else bytes(124)))
            return exp
        if opt in (P.OPT_GO, P.OPT_INFO):
            (nlen,) = struct.unpack(">I", data[:4])
            name = data[4:4 + nlen].decode(errors="replace")
            exp = exports.get(name) or (exports.get("default") if name == "" else None)
            if exp is None:
                send_opt_reply(sock, opt, P.REP_ERR_UNKNOWN, b"unknown export")
                continue
            send_opt_reply(sock, opt, P.REP_INFO,
                           struct.pack(">HQH", P.INFO_EXPORT, exp.size, exp.tflags()))
            send_opt_reply(sock, opt, P.REP_ACK)
            if opt == P.OPT_GO:
                return exp
            continue
        if opt == P.OPT_LIST:
            for name in exports:
                n = name.encode()
                send_opt_reply(sock, opt, P.REP_SERVER, struct.pack(">I", len(n)) + n)
            send_opt_reply(sock, opt, P.REP_ACK)
            continue
        if opt == P.OPT_ABORT:
            send_opt_reply(sock, opt, P.REP_ACK)
            raise ConnectionError("client aborted")
        send_opt_reply(sock, opt, P.REP_ERR_UNSUP)


def reply(sock, cookie, err=0, data=b""):
    sock.sendall(struct.pack(">IIQ", P.SIMPLE_REPLY_MAGIC, err, cookie))
    if data:
        sock.sendall(data)


def serve_conn(sock, addr, exports):
    try:
        exp = negotiate(sock, exports)
        log.info("%s connected to export %s (%s, %d bytes)", addr, exp.name, exp.path, exp.size)
        while True:
            magic, flags, cmd, cookie, offset, length = struct.unpack(
                ">IHHQQI", P.recv_exact(sock, 28))
            if magic != P.REQUEST_MAGIC:
                raise ConnectionError("bad request magic")
            if cmd == P.CMD_DISC:
                break
            # the payload cap applies to data-carrying commands only (NBD spec);
            # WRITE_ZEROES/TRIM may cover any range inside the export
            too_big = length > P.MAX_REQUEST and cmd in (P.CMD_READ, P.CMD_WRITE)
            if too_big or offset + length > exp.size:
                if cmd == P.CMD_WRITE:
                    P.recv_exact(sock, length)
                reply(sock, cookie, P.EINVAL)
                continue
            if cmd == P.CMD_READ:
                reply(sock, cookie, 0, exp.pread(length, offset))
            elif cmd == P.CMD_WRITE:
                data = P.recv_exact(sock, length)
                if exp.readonly:
                    reply(sock, cookie, P.EINVAL)
                    continue
                exp.pwrite(data, offset)
                exp.bytes_written += length
                if flags & 1:  # FUA
                    exp.fsync()
                reply(sock, cookie)
            elif cmd == P.CMD_WRITE_ZEROES:
                exp.zero(offset, length)
                if flags & 1:  # FUA
                    exp.fsync()
                reply(sock, cookie)
            elif cmd == P.CMD_FLUSH:
                exp.fsync()
                reply(sock, cookie)
            elif cmd == P.CMD_TRIM:
                reply(sock, cookie)
            else:
                reply(sock, cookie, P.EINVAL)
    except (ConnectionError, OSError) as e:
        log.info("%s disconnected: %s", addr, e)
    finally:
        sock.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export", action="append", required=True, metavar="NAME=PATH")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=10809)
    ap.add_argument("--readonly", action="store_true")
    ap.add_argument("--mask", action="append", default=[], metavar="NAME=OFFSET:LENGTH",
                    help="keep this range off the disk (boot mask), unless bootmask's flag says unmasked")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    masks = {}
    if bootmask.active():
        for spec in args.mask:
            name, rng = spec.split("=", 1)
            off, n = rng.split(":")
            masks.setdefault(name, []).append((int(off), int(n)))
    exports = {}
    for spec in args.export:
        name, path = spec.split("=", 1)
        exports[name] = Export(name, path, args.readonly, masks.get(name))
    srv = socket.socket(socket.AF_INET6 if ":" in args.bind else socket.AF_INET)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(16)
    log.info("serving %s on %s:%d", ", ".join(f"{e.name}={e.path}" for e in exports.values()),
             args.bind, args.port)
    while True:
        conn, addr = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=serve_conn, args=(conn, addr, exports), daemon=True).start()


if __name__ == "__main__":
    main()
