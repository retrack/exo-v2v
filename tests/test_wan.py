"""WAN controls (wan.py) and compressed NBD writes (nbdproto/nbdserve OPT_EXO_ZLIB).
No root, no network: files and loopback sockets.

Run: python3 -m pytest -q tests/
"""
import datetime
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
RECEIVER = os.path.join(os.path.dirname(__file__), "..", "src", "exo_v2v", "receiver")
sys.path.insert(0, RECEIVER)

import nbdproto  # noqa: E402
from exo_v2v import util, vsphere, wan  # noqa: E402
from test_bootmask import serve  # noqa: E402

MiB = 1 << 20


def at(day, hh, mm=0):
    """2026-11-02 is a Monday."""
    return datetime.datetime(2026, 11, 2 + ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(day), hh, mm)


def test_rates():
    assert wan.rate("50Mbit") == 50e6 / 8
    assert wan.rate("200 Mbps") == 200e6 / 8
    assert wan.rate("20MiB/s") == 20 * MiB
    assert wan.rate("1Gbit/s") == 1e9 / 8
    assert wan.rate("500kB/s") == 500e3
    assert wan.rate("10MBps") == 10e6
    assert wan.rate("off") is None and wan.rate(None) is None
    for bad in ("0Mbit", "fast", "12", "-5Mbit"):
        with pytest.raises(wan.WanError):
            wan.rate(bad)


def test_schedule_first_match_and_midnight():
    cfg = wan.parse({"limit": "100Mbit", "schedule": [
        {"days": "mon-fri", "hours": "08:00-18:00", "limit": "20Mbit"},
        {"days": "fri", "hours": "22:00-06:00", "limit": "off"},
        {"days": "sat,sun", "limit": "500Mbit"}]})
    assert wan.limit_at(cfg, at("mon", 9)) == (20e6 / 8, "schedule mon-fri 08:00-18:00")
    assert wan.limit_at(cfg, at("mon", 19))[0] == 100e6 / 8
    assert wan.limit_at(cfg, at("fri", 23))[0] is None           # off: no limit tonight
    assert wan.limit_at(cfg, at("sat", 3))[0] is None            # Friday's window, after midnight
    assert wan.limit_at(cfg, at("sat", 7))[0] == 500e6 / 8
    assert wan.limit_at(cfg, at("mon", 3))[0] == 100e6 / 8       # Sunday has no night window
    assert wan.next_change(cfg, at("mon", 7, 30)) == at("mon", 8)
    assert wan.parse({"schedule": [{"days": "sunday-tuesday", "limit": "1Mbit"}]})["schedule"][0][0] == {6, 0, 1}


@pytest.mark.parametrize("raw", [
    {"limit": "0Mbit"}, {"bandwidth": "1Mbit"}, {"compress": "maybe"}, {"final": "sometimes"},
    {"schedule": [{"days": "funday", "limit": "1Mbit"}]}, {"schedule": [{"hours": "8-8", "limit": "1Mbit"}]},
    {"schedule": [{"days": "mon"}]}, {"level": 12}])
def test_bad_config(raw):
    with pytest.raises((wan.WanError, ValueError)):
        wan.parse(raw)


def test_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EXO_V2V_HOME", str(tmp_path))
    assert wan.config()["limit"] is None and wan.config()["compress"] == "auto"
    (tmp_path / "config.yaml").write_text("wan:\n  limit: 80Mbit\n  compress: on\n")
    assert wan.config()["limit"] == 10e6 and wan.config()["compress"] == "on"
    (tmp_path / "config.yaml").write_text("wan:\n  limit: off\n  final: off\n  compress: off\n"
                                          "  schedule:\n    - {days: sat, limit: off}\n")
    c = wan.config()                               # YAML 1.1: bare off is false
    assert c["limit"] is None and c["final"] == "off" and c["compress"] == "off" and c["schedule"][0][2] is None
    (tmp_path / "config.yaml").write_text("wan:\n  compress: on\n")
    assert wan.config()["compress"] == "on"
    (tmp_path / "config.yaml").write_text("wan:\n  limit: 0\n")
    with pytest.raises(wan.WanError, match="config.yaml"):
        wan.config()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_bucket_paces_and_caps_debt(tmp_path):
    clock = Clock()
    b = wan.Bucket(str(tmp_path / "bucket"), clock)
    assert b.take(MiB, MiB, 2) == pytest.approx(1.0)
    clock.t += 0.5
    assert b.take(MiB // 2, MiB, 2) == pytest.approx(1.0)
    assert b.take(100 * MiB, MiB, 2) == pytest.approx(wan.DEBT_S)     # never owes more than 30 s
    clock.t += 1000
    assert b.take(MiB, MiB, 2) == 0                                    # 2 s of burst saved up
    (tmp_path / "bucket").write_text("garbage")
    assert b.take(MiB, MiB, 2) == pytest.approx(1.0)                   # reset, not a crash


def test_override_lives_with_its_owner(tmp_path):
    b = wan.Bucket(str(tmp_path / "bucket"))
    assert b.override() == (False, None)
    b.claim(5e6)
    assert b.override() == (True, 5e6)
    b.claim(None)                                  # --bwlimit off: no limit while it runs
    assert b.override() == (True, None)
    b.release()
    assert b.override() == (False, None)
    dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
    b._update(lambda d: d.update(override={"bps": 1.0, "owners": [[dead.pid, 1]]}))
    assert b.override() == (False, None)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes read-only files")
def test_a_user_reads_the_appliance_bucket(tmp_path, monkeypatch):
    path = tmp_path / "bucket"
    wan.Bucket(str(path)).claim(5e6)
    path.chmod(0o444)                              # root's /run/lock file, seen by a user
    monkeypatch.setenv("EXO_V2V_HOME", str(tmp_path / "nohome"))
    assert wan.Bucket(str(path)).override() == (True, 5e6)
    b = wan.Bucket(str(tmp_path / "ro" / "x"))     # no bucket to share: a per-user one
    assert b.take(1, 1e6, 2) < 1e-3                # a new bucket starts empty: 1 byte waits ~1 µs
    assert b.path == str(tmp_path / "nohome" / ".wan.bucket") and os.path.exists(b.path)


def test_two_processes_share_one_limit(tmp_path):
    path, rate = str(tmp_path / "bucket"), 4 * MiB
    code = ("import sys, time; sys.path.insert(0, sys.argv[1]); from exo_v2v import wan\n"
            "b = wan.Bucket(sys.argv[2]); n = 0; end = time.monotonic() + 1.5\n"
            "while time.monotonic() < end:\n"
            f"    time.sleep(b.take(64 << 10, {rate}, 0.1)); n += 64 << 10\n"
            "print(n)")
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    procs = [subprocess.Popen([sys.executable, "-c", code, src, path], stdout=subprocess.PIPE, text=True)
             for _ in range(2)]
    total = sum(int(p.communicate()[0]) for p in procs)
    assert total == pytest.approx(rate * 1.5, rel=0.15)


def test_pacer(tmp_path):
    clock, slept = Clock(), []
    cfg = wan.parse({"limit": "8Mbit"})
    p = wan.Pacer(cfg=cfg, bucket=wan.Bucket(str(tmp_path / "b"), clock), clock=clock, sleep=slept.append)
    assert p.bps == 1e6 and p.compress() and p.slice_size() == 256 << 10
    p.take(MiB)
    assert slept and p.throttled_s == pytest.approx(MiB / 1e6) and p.wire == MiB
    f = wan.Pacer(final=True, cfg=cfg, bucket=wan.Bucket(str(tmp_path / "b"), clock), clock=clock,
                  sleep=slept.append)
    n = len(slept)
    f.take(MiB)
    assert len(slept) == n                         # final cycles do not wait (wan.final: off)
    fast = wan.Pacer(cfg=wan.parse({"limit": "1Gbit"}), bucket=wan.Bucket(str(tmp_path / "b"), clock), clock=clock)
    assert not fast.compress()                     # auto: not above 50 MiB/s
    assert not wan.Pacer(cfg=wan.parse({}), bucket=wan.Bucket(str(tmp_path / "b"))).compress()


def disk(tmp_path, size=8 * MiB):
    path = str(tmp_path / "vda")
    with open(path, "wb") as f:
        f.truncate(size)
    return path


class Counting:
    def __init__(self):
        self.taken = 0

    def slice_size(self):
        return 256 << 10

    def take(self, n):
        self.taken += n


def test_compressed_writes_round_trip(tmp_path):
    path = disk(tmp_path)
    proc, port = serve(tmp_path, path)
    try:
        pacer = Counting()
        c = nbdproto.Client("127.0.0.1", port, "sda", compress=True, pacer=pacer)
        assert c.zlib
        text = b"exo-v2v " * (MiB // 8)
        z = zlib.compress(text, 1)
        c.zwrite(MiB, MiB, z)
        c.write(3 * MiB, os.urandom(MiB))
        c.flush()
        assert c.read(MiB, MiB) == text
        assert pacer.taken == 4 + len(z) + MiB
        c.close()
        plain = nbdproto.Client("127.0.0.1", port, "sda")
        assert not plain.zlib                      # never asked: never used
        with pytest.raises(IOError, match="errno 22"):
            plain.zwrite(0, MiB, z)                # not negotiated on this connection
            plain.flush()
        plain.write(0, b"x" * 4096)                # the stream is still in step
        plain.flush()
        plain.close()
    finally:
        proc.terminate()
        proc.wait()


def test_bad_compressed_writes_are_refused(tmp_path):
    path = disk(tmp_path)
    proc, port = serve(tmp_path, path)
    try:
        c = nbdproto.Client("127.0.0.1", port, "sda", compress=True)
        for zdata, length in ((b"not zlib at all", MiB),                      # corrupt
                              (zlib.compress(bytes(4 * MiB)), MiB),            # inflates past its length
                              (zlib.compress(bytes(MiB // 2)), MiB)):          # too short
            with pytest.raises(IOError, match="errno 5"):
                c.zwrite(0, length, zdata)
                c.flush()
        with pytest.raises(IOError, match="errno 22"):
            c.zwrite(7 * MiB, 2 * MiB, zlib.compress(bytes(2 * MiB)))         # past the end
            c.flush()
        c.write(0, b"y" * 4096)
        c.flush()
        assert c.read(0, 4096) == b"y" * 4096
        assert open(path, "rb").read(MiB)[4096:] == bytes(MiB - 4096)
        c.close()
        raw = socket.create_connection(("127.0.0.1", port))   # an unknown command closes the connection
        c = nbdproto.Client.__new__(nbdproto.Client)
        c.sock, c.window, c.inflight, c.cookie, c.pacer, c.zlib = raw, 16, [], 0, None, False
        c.size, c.tflags = c._handshake("sda")
        c._send(0x7777, 0, 0)
        with pytest.raises(IOError, match="errno 22"):
            c.drain()
        assert raw.recv(16) == b""
        raw.close()
    finally:
        proc.terminate()
        proc.wait()


def old_server(port, received):
    """A fixed-newstyle server that knows nothing of OPT_EXO_ZLIB (like nbdserve 0.5)."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    s, _ = srv.accept()
    s.sendall(struct.pack(">QQH", nbdproto.NBDMAGIC, nbdproto.IHAVEOPT, 3))
    nbdproto.recv_exact(s, 4)
    while True:
        _m, opt, ln = struct.unpack(">QII", nbdproto.recv_exact(s, 16))
        nbdproto.recv_exact(s, ln)
        if opt != nbdproto.OPT_GO:
            s.sendall(struct.pack(">QIII", nbdproto.REPLY_MAGIC, opt, nbdproto.REP_ERR_UNSUP, 0))
            continue
        info = struct.pack(">HQH", 0, MiB, 1 | (1 << 2) | (1 << 6))
        s.sendall(struct.pack(">QIII", nbdproto.REPLY_MAGIC, opt, nbdproto.REP_INFO, len(info)) + info)
        s.sendall(struct.pack(">QIII", nbdproto.REPLY_MAGIC, opt, nbdproto.REP_ACK, 0))
        break
    while True:
        _m, _f, cmd, cookie, off, ln = struct.unpack(">IHHQQI", nbdproto.recv_exact(s, 28))
        if cmd == nbdproto.CMD_DISC:
            break
        if cmd == nbdproto.CMD_WRITE:
            received.append((off, nbdproto.recv_exact(s, ln)))
        s.sendall(struct.pack(">IIQ", nbdproto.SIMPLE_REPLY_MAGIC, 0, cookie))
    s.close()
    srv.close()


def test_old_server_gets_plain_writes():
    with socket.socket() as t:
        t.bind(("127.0.0.1", 0))
        port = t.getsockname()[1]
    received = []
    th = threading.Thread(target=old_server, args=(port, received), daemon=True)
    th.start()
    time.sleep(0.2)
    c = nbdproto.Client("127.0.0.1", port, "sda", compress=True)
    assert not c.zlib
    sent, zeros, wire = vsphere.push_extents(Reader(b"abc" * (MiB // 3) + b"a"), c, [(0, MiB)], 2, level=1)
    c.flush()
    c.close()
    th.join(5)
    assert received and received[0][1][:3] == b"abc" and wire == sent == MiB


class Reader:
    def __init__(self, data):
        self.data = data

    def clone(self):
        return self

    def read(self, s, n):
        return self.data[s:s + n]


class Recorder:
    zlib = True

    def __init__(self):
        self.calls = []

    def write(self, off, data):
        self.calls.append(("w", off, len(data)))

    def zwrite(self, off, ln, z):
        assert zlib.decompress(z) == SAMPLE[off:off + ln]
        self.calls.append(("c", off, ln))

    def write_zeroes(self, off, ln):
        self.calls.append(("z", off, ln))


CH = vsphere.CHUNK
SAMPLE = bytes(CH) + b"text " * (CH // 5) + b"tex" + os.urandom(CH)


def test_push_extents_zero_compressed_raw():
    rec = Recorder()
    sent, zeros, wire = vsphere.push_extents(Reader(SAMPLE), rec, [(0, 3 * CH)], 2, level=1)
    assert [c[0] for c in sorted(rec.calls, key=lambda c: c[1])] == ["z", "c", "w"]
    assert zeros == CH and sent == 2 * CH and CH < wire < 2 * CH
    rec = Recorder()
    rec.zlib = False                               # the receiver did not agree: plain writes
    sent, zeros, wire = vsphere.push_extents(Reader(SAMPLE), rec, [(0, 3 * CH)], 2, level=1)
    assert [c[0] for c in sorted(rec.calls, key=lambda c: c[1])] == ["z", "w", "w"] and wire == sent


def test_incompressible_data_is_only_sampled():
    probe, tried = {"miss": 0}, []
    real = vsphere.zlib.compress
    vsphere.zlib.compress = lambda b, lv: tried.append(1) or real(b, lv)
    try:
        for i in range(24):
            assert vsphere._prepare(os.urandom(CH), 1, probe)[0][2] == "r"
        assert probe["miss"] == 24 and len(tried) == 8 + 2      # 8 tries, then 1 in 8
        repeated = os.urandom(16 << 10) * (CH // (16 << 10))   # within zlib's 32 KiB window
        assert vsphere._prepare(repeated, 1, probe)[0][2] == "c" and probe["miss"] == 0
    finally:
        vsphere.zlib.compress = real


def test_transfer_summary():
    cyc = {"disks": {"a": {"sent": 100 * MiB, "wire": 31 * MiB, "compression": "zlib1", "throttled_s": 40},
                     "b": {"sent": 0, "wire": 0, "compression": "off"}}}
    assert util.transfer(cyc) == ", 31.0 MiB sent (compressed x0.31), throttled 40s"
    assert util.transfer({"disks": {"a": {"sent": MiB}}}) == ""
