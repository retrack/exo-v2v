"""Minimal NBD protocol (fixed newstyle) shared by nbdserve.py and nbdpush.py.

Pure Python, standard library only, so it runs in a RAM-booted receiver and on
any sender host. Implements only what the migration data path needs:
OPT_EXPORT_NAME, OPT_GO, OPT_INFO, OPT_LIST, OPT_ABORT; simple replies;
READ, WRITE, FLUSH, TRIM, WRITE_ZEROES, DISC. Structured replies are refused,
so real clients (qemu-nbd, nbdcopy, nbd-client) fall back to simple replies.
Spec: https://github.com/NetworkBlockDevice/nbd/blob/master/doc/proto.md
"""
import socket
import struct

NBDMAGIC = 0x4E42444D41474943
IHAVEOPT = 0x49484156454F5054
REPLY_MAGIC = 0x3E889045565A9
REQUEST_MAGIC = 0x25609513
SIMPLE_REPLY_MAGIC = 0x67446698

FLAG_FIXED_NEWSTYLE = 1 << 0
FLAG_NO_ZEROES = 1 << 1

OPT_EXPORT_NAME = 1
OPT_ABORT = 2
OPT_LIST = 3
OPT_INFO = 6
OPT_GO = 7

REP_ACK = 1
REP_SERVER = 2
REP_INFO = 3
REP_ERR_UNSUP = (1 << 31) + 1
REP_ERR_INVALID = (1 << 31) + 3
REP_ERR_UNKNOWN = (1 << 31) + 6

INFO_EXPORT = 0

TF_HAS_FLAGS = 1 << 0
TF_READ_ONLY = 1 << 1
TF_SEND_FLUSH = 1 << 2
TF_SEND_FUA = 1 << 3
TF_SEND_TRIM = 1 << 5
TF_SEND_WRITE_ZEROES = 1 << 6

CMD_READ = 0
CMD_WRITE = 1
CMD_DISC = 2
CMD_FLUSH = 3
CMD_TRIM = 4
CMD_WRITE_ZEROES = 6

EIO = 5
EINVAL = 22
ENOSPC = 28

MAX_REQUEST = 32 * 1024 * 1024


def recv_exact(sock, n):
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        r = sock.recv_into(view[got:], n - got)
        if r == 0:
            raise ConnectionError("peer closed connection")
        got += r
    return bytes(buf)


class Client:
    """Blocking NBD client with a simple in-order request pipeline."""

    def __init__(self, host, port, export, window=16, timeout=300):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.window = window
        self.inflight = []  # (cookie, cmd, length)
        self.cookie = 0
        self.size, self.tflags = self._handshake(export)

    def _handshake(self, export):
        magic, opt = struct.unpack(">QQ", recv_exact(self.sock, 16))
        if magic != NBDMAGIC or opt != IHAVEOPT:
            raise ConnectionError("not a fixed-newstyle NBD server")
        (hflags,) = struct.unpack(">H", recv_exact(self.sock, 2))
        cflags = FLAG_FIXED_NEWSTYLE | (hflags & FLAG_NO_ZEROES)
        self.sock.sendall(struct.pack(">I", cflags))
        name = export.encode()
        data = struct.pack(">I", len(name)) + name + struct.pack(">H", 0)
        self.sock.sendall(struct.pack(">QII", IHAVEOPT, OPT_GO, len(data)) + data)
        size = tflags = None
        while True:
            rmagic, ropt, rtype, rlen = struct.unpack(">QIII", recv_exact(self.sock, 20))
            payload = recv_exact(self.sock, rlen) if rlen else b""
            if rmagic != REPLY_MAGIC:
                raise ConnectionError("bad option reply magic")
            if rtype == REP_INFO:
                (itype,) = struct.unpack(">H", payload[:2])
                if itype == INFO_EXPORT:
                    size, tflags = struct.unpack(">QH", payload[2:12])
            elif rtype == REP_ACK:
                break
            elif rtype & (1 << 31):
                raise ConnectionError(f"server refused export {export!r} (0x{rtype:x}) {payload!r}")
        if size is None:
            raise ConnectionError("server sent no export info")
        return size, tflags

    def _send(self, cmd, offset, length, data=b""):
        while len(self.inflight) >= self.window:
            self._reap_one()
        self.cookie += 1
        hdr = struct.pack(">IHHQQI", REQUEST_MAGIC, 0, cmd, self.cookie, offset, length)
        self.sock.sendall(hdr + data if len(data) < 65536 else hdr)
        if len(data) >= 65536:
            self.sock.sendall(data)
        self.inflight.append((self.cookie, cmd, length))
        return self.cookie

    def _reap_one(self):
        cookie, cmd, length = self.inflight.pop(0)
        magic, err, rcookie = struct.unpack(">IIQ", recv_exact(self.sock, 16))
        if magic != SIMPLE_REPLY_MAGIC or rcookie != cookie:
            raise ConnectionError("unexpected reply (out of order or structured)")
        data = recv_exact(self.sock, length) if cmd == CMD_READ and not err else b""
        if err:
            raise IOError(f"NBD command {cmd} failed with errno {err}")
        return data

    def drain(self):
        while self.inflight:
            self._reap_one()

    def write(self, offset, data):
        self._send(CMD_WRITE, offset, len(data), data)

    def write_zeroes(self, offset, length):
        if self.tflags & TF_SEND_WRITE_ZEROES:
            self._send(CMD_WRITE_ZEROES, offset, length)
        else:
            step = 4 * 1024 * 1024
            for o in range(offset, offset + length, step):
                n = min(step, offset + length - o)
                self._send(CMD_WRITE, o, n, bytes(n))

    def read(self, offset, length):
        self.drain()
        self._send(CMD_READ, offset, length)
        return self._reap_one()

    def flush(self):
        self.drain()
        if self.tflags & TF_SEND_FLUSH:
            self._send(CMD_FLUSH, 0, 0)
            self.drain()

    def close(self):
        try:
            self.drain()
            self.sock.sendall(struct.pack(">IHHQQI", REQUEST_MAGIC, 0, CMD_DISC, 0, 0, 0))
        finally:
            self.sock.close()
