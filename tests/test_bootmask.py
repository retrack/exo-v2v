"""Boot mask (receiver/bootmask.py): the first and last MiB of the system disk
stay off the disk until the conversion. No root needed: files stand in for disks.

Run: python3 -m pytest -q tests/
"""
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
RECEIVER = os.path.join(os.path.dirname(__file__), "..", "src", "exo_v2v", "receiver")
sys.path.insert(0, RECEIVER)

import bootmask  # noqa: E402
import nbdproto  # noqa: E402
from exo_v2v import userdata  # noqa: E402

MiB = 1 << 20
SIZE = 8 * MiB
RANGES = [(0, MiB), (SIZE - MiB, MiB)]


def disk(tmp_path, name="disk.img"):
    path = tmp_path / name
    data = bytes((i * 7 + 3) % 251 for i in range(SIZE))
    path.write_bytes(data)
    return str(path), bytearray(data)


def test_mask_moves_the_ranges_off_the_disk_and_overlays_them(tmp_path):
    path, logical = disk(tmp_path)
    md = str(tmp_path / "mask")
    fd = os.open(path, os.O_RDWR)
    m = bootmask.Mask("sda", RANGES, fd, md)
    raw = open(path, "rb").read()
    assert raw[:MiB] == bytes(MiB) and raw[-MiB:] == bytes(MiB)           # nothing to boot on the disk
    assert raw[MiB:-MiB] == logical[MiB:-MiB]
    assert open(bootmask.side_path(md, "sda", 0), "rb").read() == logical[:MiB]
    assert m.read(fd, 0, SIZE) == logical                                 # reads see the replica
    # a write across both edges of the first range, and one across the last range's start
    for off, n in ((MiB - 4096, 8192), (SIZE - MiB - 512, 1024)):
        data = os.urandom(n)
        m.write(fd, data, off)
        logical[off:off + n] = data
    m.zero(fd, MiB - 100, 200, lambda o, n: os.pwrite(fd, bytes(n), o))
    logical[MiB - 100:MiB + 100] = bytes(200)
    assert m.read(fd, 0, SIZE) == logical
    raw = open(path, "rb").read()
    assert raw[:MiB] == bytes(MiB) and raw[-MiB:] == bytes(MiB)
    # a restarted NBD server keeps the side files (and zeroes the disk again)
    m.close()
    m2 = bootmask.Mask("sda", RANGES, fd, md)
    assert m2.read(fd, 0, SIZE) == logical
    m2.close()
    # read-only overlay for rootcopy and /hashmap
    ov = bootmask.reader("sda", RANGES, fd, md)
    assert ov.read(fd, 0, SIZE) == logical
    ov.close()
    # unmask: the disk gets the boot sectors, once
    assert bootmask.unmask({"sda": path}, {"sda": RANGES}, md)
    assert open(path, "rb").read() == logical
    assert not bootmask.active(md) and bootmask.reader("sda", RANGES, fd, md) is None
    os.pwrite(fd, b"converted", 0)                       # e.g. virt-v2v rewrote the boot loader
    assert bootmask.unmask({"sda": path}, {"sda": RANGES}, md) == "already unmasked"
    assert open(path, "rb").read(9) == b"converted"
    os.close(fd)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(tmp_path, path, masks=(), port=None):
    port = port or free_port()
    env = dict(os.environ, V2V_MASK_DIR=str(tmp_path / "mask"))
    args = [sys.executable, "-I", os.path.join(RECEIVER, "nbdserve.py"), "--port", str(port),
            "--export", f"sda={path}"] + [a for off, n in masks for a in ("--mask", f"sda={off}:{n}")]
    proc = subprocess.Popen(args, env=env, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return proc, port
        except OSError:
            time.sleep(0.05)
    proc.kill()
    pytest.fail("nbdserve did not start")


def test_nbdserve_masks_writes_and_reads(tmp_path):
    path = str(tmp_path / "vda")
    with open(path, "wb") as f:
        f.truncate(SIZE)
    proc, port = serve(tmp_path, path, RANGES)
    try:
        c = nbdproto.Client("127.0.0.1", port, "sda")
        data = os.urandom(SIZE)
        for off in range(0, SIZE, MiB):
            c.write(off, data[off:off + MiB])
        c.write_zeroes(3 * MiB, MiB)
        c.flush()
        want = data[:3 * MiB] + bytes(MiB) + data[4 * MiB:]
        assert c.read(0, 2 * MiB) == want[:2 * MiB]
        assert c.read(SIZE - 2 * MiB, 2 * MiB) == want[-2 * MiB:]
        c.close()
    finally:
        proc.terminate()
        proc.wait()
    raw = open(path, "rb").read()
    assert raw[:MiB] == bytes(MiB) and raw[-MiB:] == bytes(MiB) and raw[MiB:-MiB] == want[MiB:-MiB]
    bootmask.unmask({"sda": path}, {"sda": RANGES}, str(tmp_path / "mask"))
    assert open(path, "rb").read() == want
    # once unmasked, the server serves the disk as it is (no zeroing, no side files)
    proc, port = serve(tmp_path, path, RANGES)
    try:
        c = nbdproto.Client("127.0.0.1", port, "sda")
        assert c.read(0, MiB) == want[:MiB]
        c.close()
    finally:
        proc.terminate()
        proc.wait()
    assert open(path, "rb").read() == want


def test_rootcopy_reads_through_the_mask(tmp_path):
    src, logical = disk(tmp_path, "src.img")
    md = tmp_path / "mask"
    fd = os.open(src, os.O_RDWR)
    bootmask.Mask("sda", RANGES, fd, str(md)).close()      # the production receiver's mask
    os.close(fd)
    cfg = tmp_path / "mask.json"
    cfg.write_text(json.dumps({"sda": [list(r) for r in RANGES]}))
    dst = str(tmp_path / "dst.img")
    with open(dst, "wb") as f:
        f.truncate(SIZE)
    proc, port = serve(tmp_path / "other", dst)              # the copy's receiver, unmasked here
    try:
        env = dict(os.environ, V2V_MASK_DIR=str(md))
        out = subprocess.run([sys.executable, "-I", os.path.join(RECEIVER, "rootcopy.py"), src,
                              f"127.0.0.1:{port}", "sda", "2", "--overlay", "sda", "--mask-config", str(cfg)],
                             env=env, capture_output=True, text=True, timeout=60)
    finally:
        proc.terminate()
        proc.wait()
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1])["overlay"] is True
    assert open(dst, "rb").read() == logical


def test_payload_carries_the_mask_and_version():
    p = {"disks": [{"export": "sda", "role": "root", "capacity": 40 << 30},
                   {"export": "sdb", "role": "volume", "capacity": 10 << 30}]}
    m = userdata.boot_mask(p)
    assert m == {"sda": [[0, MiB], [(40 << 30) - MiB, MiB]]}
    tar = tarfile.open(fileobj=io.BytesIO(userdata.payload_tgz(masks=m)))
    names = tar.getnames()
    assert {"bootmask.py", "mask.json", "version.json"} <= set(names)
    assert json.load(tar.extractfile("mask.json")) == m
    assert userdata.boot_mask({"disks": []}) == {}


# ---------------- appliance side: egress, a receiver that restarted, change IDs ----------------

class FakeExo:
    def __init__(self, state="running"):
        self.rules, self.state, self.stopped, self.n = [], state, [], 0

    def sg_egress_rules(self, sg):
        return list(self.rules)

    def sg_rule_add(self, sg, cidr, port, proto="tcp", desc="", flow="ingress"):
        self.n += 1
        self.rules.append({"id": f"r{self.n}", "description": desc, "network": cidr, "flow": flow})

    def sg_rule_delete(self, sg, rid):
        self.rules = [x for x in self.rules if x["id"] != rid]

    def instance(self, name):
        return {"state": self.state}

    def instance_stop(self, name):
        self.stopped.append(name)


def migration(tmp_path, monkeypatch, mode="template"):
    from exo_v2v.state import State
    monkeypatch.setenv("EXO_V2V_HOME", str(tmp_path / "home"))
    st = State.new("m1", {"receiver_mode": mode, "disks": [{"disk": "scsi0:0", "export": "sda", "role": "root"},
                                                          {"disk": "scsi0:1", "export": "sdb", "role": "volume"}]})
    st.data["receiver"] = {"name": "v2v-m1", "sg": "exo-v2v-m1", "ip": "192.0.2.10", "volumes": [
        {"name": "v2v-m1-d1", "export": "sdb"}]}
    st.save()
    return st


def test_egress_closed_until_the_cutover(tmp_path, monkeypatch):
    from exo_v2v import cli
    st, ex = migration(tmp_path, monkeypatch), FakeExo()
    ex.rules.append({"id": "user", "description": "someone's rule", "network": "0.0.0.0/0"})
    cli.close_egress(st, ex)
    cli.close_egress(st, ex)                                  # idempotent
    assert [x["description"] for x in ex.rules].count(cli.EGRESS_CLOSED) == 1
    assert st["receiver"]["egress_closed"]
    ex.rules.append({"id": "rh", "description": "exo-v2v: rehearsal shop relay", "network": "198.51.100.1/32"})
    cli.open_egress(st, ex)                                   # every exo-v2v rule goes, not the others
    assert [x["id"] for x in ex.rules] == ["user"] and not st["receiver"]["egress_closed"]
    stock, ex2 = migration(tmp_path / "s", monkeypatch, mode="stock"), FakeExo()
    cli.close_egress(stock, ex2)                              # a stock receiver needs mirrors on a reset
    assert ex2.rules == []


def test_receiver_down_tells_restart_from_booted_copy(tmp_path, monkeypatch):
    from exo_v2v import cli
    st = migration(tmp_path, monkeypatch)
    with open(os.path.join(st.dir, "known_hosts"), "w") as f:
        f.write("|1|abc= ssh-ed25519 AGENTKEY\n")
    monkeypatch.setattr(cli, "host_key", lambda ip, timeout=8: None)
    with pytest.raises(SystemExit):
        cli.receiver_down(st, FakeExo(), "timeout")
    assert st["receiver"]["down"]["trust_volumes"] is True       # masked: nothing booted
    monkeypatch.setattr(cli, "host_key", lambda ip, timeout=8: "GUESTKEY")
    ex = FakeExo()
    with pytest.raises(SystemExit):
        cli.receiver_down(st, ex, "host key changed")
    assert ex.stopped == ["v2v-m1"] and st["receiver"]["down"]["trust_volumes"] is False
    with pytest.raises(SystemExit):
        cli.receiver_down(st, FakeExo(state="stopped"), "refused")
    assert st["receiver"]["down"]["state"] == "stopped"


class FakeTunnel:
    def __init__(self, meta=None):
        self.meta, self.posted = dict(meta or {}), []

    def get(self, path, timeout=600):
        return self.meta.get(path.split("export=")[1], {})

    def post(self, path, body=None, timeout=600):
        self.posted.append((path.split("export=")[1], body))


def test_agent_restart_in_the_same_boot_keeps_the_change_ids(tmp_path, monkeypatch):
    from exo_v2v import cli
    st = migration(tmp_path, monkeypatch)
    r = st["receiver"]
    r.update(agent_started=time.time() - 3600, boot_id="b1",
             meta={"sda": {"change_id": "52 a/1"}, "sdb": {"change_id": "52 b/7"}})
    t = FakeTunnel()
    cli.check_receiver_continuity(st, t, {"uptime_s": 30, "boot_id": "b1"})
    assert sorted(e for e, _ in t.posted) == ["sda", "sdb"]
    r["agent_started"] = time.time() - 3600
    t = FakeTunnel()
    cli.check_receiver_continuity(st, t, {"uptime_s": 30, "boot_id": "b2"})   # another boot: full copy
    assert t.posted == []


def test_public_only_group_member_records_its_nic(tmp_path, monkeypatch):
    """0.3.0-0.4.0: a member whose only network is the public interface never
    recorded its NICs, so the group preflight and the conversion refused it."""
    from exo_v2v import cli
    st = migration(tmp_path, monkeypatch)
    st.data["plan"]["networks"] = [{"network": "public", "mode": "dhcp", "source_mac": "00:0c:29:00:00:01"}]

    class Ex(FakeExo):
        def instance_nics(self, name):
            return [{"network": "public", "mac": "06:00:00:00:00:01"}]

    cli.attach_networks(st, Ex())
    assert st["receiver"]["nics"] == [{"network": "public", "mac": "06:00:00:00:00:01"}]
    assert cli.netmap_of(st) == [{"mac": "06:00:00:00:00:01", "mode": "dhcp"}]
