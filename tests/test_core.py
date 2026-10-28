"""Unit tests that need neither root, vSphere nor Exoscale.

Run: python3 -m pytest -q tests/   (sgdisk/sfdisk needed for the partition tests)
"""
import os
import shutil
import struct
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "exo_v2v", "receiver"))

from exo_v2v import plan  # noqa: E402
import guestfix  # noqa: E402

GiB = 1 << 30


def inv(**kw):
    base = {"name": "vm1", "guest_id": "rhel9_64Guest", "firmware": "bios", "secure_boot": False,
            "cpus": 2, "memory_mb": 4096, "cbt_enabled": True, "cbt_disks": {}, "snapshots": [],
            "tools": "ok", "nics": [{"label": "n1"}],
            "disks": [{"id": "scsi0:0", "label": "Hard disk 1", "capacity": 40 * GiB, "file": "[ds] vm1/vm1.vmdk",
                       "thin": True, "change_id": None, "mode": "persistent"},
                      {"id": "scsi0:1", "label": "Hard disk 2", "capacity": 10 * GiB + 1, "file": "[ds] vm1/vm1_1.vmdk",
                       "thin": True, "change_id": None, "mode": "persistent"}]}
    base.update(kw)
    return base


# ---------------- plan ----------------

def test_plan_sizes_and_types():
    p = plan.build(inv(), "ch-gva-2")
    assert plan.build(inv(disks=[inv()["disks"][0], dict(inv()["disks"][1], capacity=10 * GiB)]),
                      "z")["volumes_gb"] == [11]                       # exact 10 GiB still gets slack
    assert p["errors"] == []
    assert p["target_type"] == "standard.medium" and p["convert_type"] == "standard.large"
    assert p["root_gb"] == 40 and p["volumes_gb"] == [11]            # 10 GiB + 1 B + 1 MiB slack
    assert [d["export"] for d in p["disks"]] == ["sda", "sdb"]
    assert p["template"].startswith("Linux Debian 13") and p["receiver_user"] == "debian"


def test_plan_uefi_uses_ubuntu_template():
    p = plan.build(inv(firmware="efi"), "ch-gva-2")
    assert "Ubuntu 24.04" in p["template"] and p["receiver_user"] == "ubuntu"


def test_plan_big_vm_keeps_its_type_for_conversion():
    p = plan.build(inv(cpus=8, memory_mb=32768), "ch-gva-2")
    assert p["target_type"] == p["convert_type"] == "standard.huge"


def test_plan_blockers():
    d = inv()["disks"][0]
    many = [dict(d, id=f"scsi0:{i}") for i in range(7)]
    assert any("Block Storage volumes" in e for e in plan.build(inv(disks=many), "z")["errors"])
    indep = [dict(d, mode="independent_persistent")]
    assert any("independent" in e for e in plan.build(inv(disks=indep), "z")["errors"])
    rdm = [dict(d, file="")]
    assert any("RDM" in e for e in plan.build(inv(disks=rdm), "z")["errors"])


def test_plan_system_disk_override_reorders():
    p = plan.build(inv(), "z", system_disk="scsi0:1")
    assert p["disks"][0]["disk"] == "scsi0:1" and p["disks"][0]["role"] == "root"


def test_plan_ignores_own_snapshots():
    p = plan.build(inv(snapshots=["v2v-sync-123", "before-upgrade"]), "z")
    assert any("before-upgrade" in w for w in p["warnings"])
    assert not any("v2v-sync" in w for w in p["warnings"])


def test_instance_type_for():
    assert plan.instance_type_for(1, 1024) == "standard.tiny"
    assert plan.instance_type_for(4, 12288) == "standard.extra-large"
    assert plan.instance_type_for(64, 999999) == "standard.jumbo"


# ---------------- vsphere helpers (no connection) ----------------

def test_coalesce_and_flat_path():
    pytest.importorskip("pyVmomi")
    from exo_v2v import vsphere as V
    assert V.coalesce([(0, 10), (100, 10)], gap=1000) == [(0, 110)]
    assert V.coalesce([(0, 10), (1 << 30, 10)], gap=1000) == [(0, 10), (1 << 30, 10)]
    big = V.coalesce([(0, 200 << 20)])
    assert all(n <= V.MAX_RANGE for _s, n in big) and sum(n for _s, n in big) == 200 << 20
    assert V.flat_path("[datastore1] vm/vm_1.vmdk") == ("datastore1", "vm/vm_1-flat.vmdk")


# ---------------- guestfix: tables + probes on image files ----------------

needs_sgdisk = pytest.mark.skipif(not shutil.which("sgdisk") or not shutil.which("sfdisk"),
                                  reason="sgdisk/sfdisk not installed")


def mkimg(tmp_path, kind, size=256 << 20):
    img = str(tmp_path / f"{kind}.img")
    with open(img, "wb") as f:
        f.truncate(size)
    if kind == "gpt":
        subprocess.run(["sgdisk", "-o", "-n", "1:2048:+64M", "-n", "2:0:0", img], check=True, capture_output=True)
    else:
        subprocess.run(["sfdisk", "-q", img], input="2048,131072,83\n133120,,83\n", text=True, check=True)
    return img


def put_xfs(img, off, sectsz):
    with open(img, "r+b") as f:
        sb = bytearray(4096)
        sb[0:4] = b"XFSB"
        struct.pack_into(">H", sb, 102, sectsz)
        f.seek(off); f.write(sb)


def put_ext(img, off, log_bs):
    with open(img, "r+b") as f:
        f.seek(off + 1024 + 24); f.write(struct.pack("<I", log_bs))
        f.seek(off + 1024 + 56); f.write(struct.pack("<H", 0xEF53))


@needs_sgdisk
def test_gpt_rescale_on_file(tmp_path, monkeypatch):
    img = mkimg(tmp_path, "gpt")
    put_ext(img, 2048 * 512, 2)             # ext4, 4K blocks
    put_xfs(img, (2048 + 131072) * 512, 512)  # xfs sectsz=512
    monkeypatch.setenv("V2V_TEST_LBS", "4096")
    # same-size target: the last partition reaches the 512 last-usable LBA -> refused, clearly
    assert "must be >= 1 MiB larger" in " ".join(guestfix.adapt(img)["problems"])
    with open(img, "r+b") as f:          # volume with slack (what plan.py provisions)
        f.truncate(os.path.getsize(img) + (1 << 20))
    r = guestfix.analyze(img)
    assert r["layout"] == "gpt" and r["table_sector"] == 512 and r["needs_rescale"]
    assert [f["type"] for f in r["filesystems"]] == ["ext", "xfs"]
    assert not r["compatible"] and "xfs sectsz=512" in r["problems"][0]
    a = guestfix.adapt(img)
    assert a["action"] == "gpt rescaled 512->4096"
    g = guestfix.read_gpt(img, 4096)
    assert g and [(p["first"], p["last"]) for p in g["parts"]][0] == (256, 256 + 16384 - 1)
    # GPT CRCs + layout must validate under a 4096-byte sector view (needs a loop device)
    if shutil.which("sudo") and subprocess.run(["sudo", "-n", "true"]).returncode == 0:
        loop = subprocess.run(["sudo", "losetup", "-f", "--show", "-r", "--sector-size", "4096", img],
                              capture_output=True, text=True, check=True).stdout.strip()
        try:
            v = subprocess.run(["sudo", "sgdisk", "-v", loop], capture_output=True, text=True)
            assert "No problems found" in v.stdout, v.stdout
        finally:
            subprocess.run(["sudo", "losetup", "-d", loop])
    assert guestfix.adapt(img)["action"] == "none"          # idempotent


@needs_sgdisk
def test_mbr_unit_inference(tmp_path, monkeypatch):
    img = mkimg(tmp_path, "mbr")
    put_ext(img, 2048 * 512, 2)
    monkeypatch.setenv("V2V_TEST_LBS", "4096")
    m = guestfix.read_mbr(img)
    assert guestfix.mbr_unit(img, m) == 512          # ext found at start*512
    a = guestfix.adapt(img)
    assert a["action"] == "mbr rescaled 512->4096"
    m2 = guestfix.read_mbr(img)
    assert guestfix.mbr_unit(img, m2) == 4096        # now found at start*4096
    assert m2["parts"][0]["start"] == 256


def test_probe_fs(tmp_path):
    img = str(tmp_path / "x.img")
    open(img, "wb").truncate(1 << 20)
    put_xfs(img, 0, 4096)
    assert guestfix.probe_fs(img, 0)[:2] == ("xfs", True)
    put_xfs(img, 0, 512)
    assert guestfix.probe_fs(img, 0)[:2] == ("xfs", False)
    open(img, "wb").truncate(1 << 20)
    put_ext(img, 0, 0)                       # 1K blocks
    assert guestfix.probe_fs(img, 0)[:2] == ("ext", False)


def test_relayout_refuses_ntfs(tmp_path, monkeypatch):
    src, spare = str(tmp_path / "src.img"), str(tmp_path / "spare.img")
    for p in (src, spare):
        open(p, "wb").truncate(64 << 20)
    with open(src, "r+b") as f:                       # whole-disk NTFS, 512 bytes/sector
        bs = bytearray(512); bs[3:11] = b"NTFS    "; struct.pack_into("<H", bs, 11, 512); f.write(bs)
    monkeypatch.setenv("V2V_TEST_LBS", "4096")
    with pytest.raises(ValueError, match="cannot rebuild partition 0 \\(ntfs"):
        guestfix.relayout(src, spare)
    assert not any(open(spare, "rb").read(1 << 20))  # spare untouched


@needs_sgdisk
def test_windows_msr_at_lba34_is_realigned(tmp_path, monkeypatch):
    img = str(tmp_path / "win.img")
    open(img, "wb").truncate(256 << 20)
    # Windows layout: MSR 16 MiB at LBA 34, data partition 1 MiB-aligned after it
    subprocess.run(["sgdisk", "-o", "-a", "1", "-n", "1:34:32801", "-t", "1:0C01",
                    "-n", "2:34816:0", "-t", "2:0700", img], check=True, capture_output=True)
    open(img, "r+b").truncate((256 << 20) + (1 << 20))     # volume with slack, as provisioned
    monkeypatch.setenv("V2V_TEST_LBS", "4096")
    r = guestfix.analyze(img)
    assert not any("not 4 KiB aligned" in p for p in r["problems"]), r["problems"]
    a = guestfix.adapt(img)
    assert a["action"] == "gpt rescaled 512->4096", a["problems"]
    parts = {p["partition"]: p for p in a["rescaled"]}
    assert parts[1]["first"] == 6 and parts[1]["last"] == (32801 + 1) // 8 - 1   # MSR shrunk inward (>= first usable)
    assert parts[2]["first"] == 34816 // 8                                      # data partition exact


def _ntfs_bs(bps=512, spc=8, total=None, size=64 << 20):
    bs = bytearray(512)
    bs[3:11] = b"NTFS    "
    struct.pack_into("<H", bs, 11, bps); bs[13] = spc
    struct.pack_into("<Q", bs, 0x28, total if total is not None else size // bps - 1)
    bs[510:512] = b"\x55\xaa"
    return bs


def test_ntfs_rebps_fields_and_backup(tmp_path):
    img = str(tmp_path / "n.img"); size = 64 << 20
    open(img, "wb").truncate(size)
    with open(img, "r+b") as f:
        f.write(_ntfs_bs())
    r = guestfix.ntfs_rebps(img, 0, size)
    assert r["action"] == "ntfs bps 512->4096" and bytes.fromhex(r["orig_boot_sector"])[11:13] == b"\x00\x02"
    bs = open(img, "rb").read(512)
    assert struct.unpack_from("<H", bs, 11)[0] == 4096 and bs[13] == 1
    assert struct.unpack_from("<Q", bs, 0x28)[0] == size // 4096 - 1
    with open(img, "rb") as f:                      # backup boot sector in the last 4K sector
        f.seek(size - 4096); assert f.read(512) == bs
    assert guestfix.ntfs_rebps(img, 0, size)["action"] == "none"          # idempotent
    assert guestfix.probe_fs(img, 0)[:2] == ("ntfs", True)


def test_ntfs_rebps_refuses_small_clusters(tmp_path):
    img = str(tmp_path / "n2.img"); size = 64 << 20
    open(img, "wb").truncate(size)
    with open(img, "r+b") as f:
        f.write(_ntfs_bs(spc=1))                     # 512 B clusters: cannot be expressed in 4K sectors
    with pytest.raises(ValueError, match="cluster size 512"):
        guestfix.ntfs_rebps(img, 0, size)


def test_plan_carries_secure_boot_and_tpm():
    p = plan.build(inv(firmware="efi", secure_boot=True, vtpm=True), "ch-gva-2")
    assert p["secure_boot"] and p["tpm"] and p["errors"] == []
    assert not plan.build(inv(firmware="bios", secure_boot=True), "z")["secure_boot"]
    assert not plan.build(inv(), "z")["tpm"]


def test_plan_receiver_template_mode():
    p = plan.build(inv(firmware="efi"), "ch-gva-2", receiver_template="exo-v2v-linux-uefi",
                   receiver_template_visibility="private", receiver_template_boot_mode="uefi")
    assert (p["receiver_mode"], p["template"], p["template_visibility"], p["receiver_user"]) == \
        ("template", "exo-v2v-linux-uefi", "private", "v2v")
    assert p["errors"] == []
    bad = plan.build(inv(firmware="bios"), "z", receiver_template="exo-v2v-linux-uefi",
                     receiver_template_boot_mode="uefi")
    assert any("legacy" in e for e in bad["errors"])
    assert plan.build(inv(), "z")["receiver_mode"] == "kexec"


def test_failed_instance_create_drops_temp_ssh_key(monkeypatch):
    from exo_v2v import exo as exomod
    monkeypatch.setattr(exomod.shutil, "which", lambda _: "/usr/bin/exo")
    calls = []

    def fake_run(self, *args, json_out=True, check=True, stdin=None):
        calls.append(args)
        if args[:3] == ("compute", "instance", "create"):
            raise exomod.ExoError("Forbidden")
        if args[:3] == ("compute", "ssh-key", "list"):
            return [{"name": "v2v-web-1792403445"}, {"name": "v2v-web-db-1792403445"},
                    {"name": "v2v-web-old"}, {"name": "acr"}]
        return ""
    monkeypatch.setattr(exomod.Exo, "run", fake_run)
    ex = exomod.Exo("ch-gva-2")
    with pytest.raises(exomod.ExoError):
        ex.instance_create("v2v-web", "t", "standard.small", 10, [], "ud", {"exo-v2v": "web"})
    deleted = [a[3] for a in calls if a[:3] == ("compute", "ssh-key", "delete")]
    assert deleted == ["v2v-web-1792403445"]


# ---------------- system disk, folds, disk order ----------------

def disk(id_, cap_gib, label=None):
    return {"id": id_, "label": label or id_, "capacity": cap_gib * GiB, "file": f"[ds] vm1/{id_}.vmdk",
            "thin": True, "change_id": None, "mode": "persistent"}


def test_plan_system_disk_from_inventory_not_alphabetical():
    # nvme0:0 sorts before scsi0:0: the system disk must come from the boot order / Tools
    i = inv(disks=[disk("nvme0:0", 8), disk("scsi0:0", 40), disk("scsi0:1", 10)],
            boot_disk="scsi0:0", boot_disk_source="boot order")
    p = plan.build(i, "z")
    assert [d["disk"] for d in p["disks"]] == ["scsi0:0", "scsi0:1", "nvme0:0"]
    assert p["disks"][0]["role"] == "root" and p["root_gb"] == 40
    assert any("from the boot order" in n for n in p["notes"])


def test_plan_warns_when_system_disk_is_guessed():
    i = inv(disks=[disk("sata0:0", 8), disk("scsi0:0", 40)], boot_disk="scsi0:0",
            boot_disk_source="controller order")
    assert any("is a guess" in w for w in plan.build(i, "z")["warnings"])


def test_plan_folds_disks_sharing_a_filesystem_with_the_system_disk():
    i = inv(disks=[disk("scsi0:0", 40), disk("scsi0:1", 14), disk("scsi1:0", 6), disk("scsi1:1", 3)],
            boot_disk="scsi0:0", boot_disk_source="boot order",
            guest_mounts=[{"path": "/", "disks": ["scsi0:0", "scsi1:0"]},
                          {"path": "/var", "disks": ["scsi1:0", "scsi1:1"]},     # transitively
                          {"path": "/data", "disks": ["scsi0:1"]}])
    p = plan.build(i, "z")
    roles = {d["disk"]: d["role"] for d in p["disks"]}
    assert roles == {"scsi0:0": "root", "scsi0:1": "volume", "scsi1:0": "fold", "scsi1:1": "fold"}
    f = {x["disk"]: x for x in p["folds"]}
    assert f["scsi1:0"]["offset"] == 40 * GiB and f["scsi1:1"]["offset"] == 46 * GiB
    assert p["root_gb"] == 50                                        # 40 + 6 + 3 + 1 MiB for a backup GPT
    assert p["volumes_gb"] == [15]
    assert plan.build(i, "z", auto_fold=False)["folds"] == []
    assert [x["disk"] for x in plan.build(i, "z", auto_fold=False, fold=["scsi0:1"])["folds"]] == ["scsi0:1"]


def test_plan_fold_allows_more_than_five_data_disks():
    many = [disk("scsi0:0", 20)] + [disk(f"scsi1:{u}", 1) for u in range(7)]
    i = inv(disks=many, boot_disk="scsi0:0", boot_disk_source="boot order")
    assert any("at most 5" in e for e in plan.build(i, "z")["errors"])
    assert not plan.build(i, "z", fold=["scsi1:5", "scsi1:6"])["errors"]


def test_fold_layout_alignment():
    at, gb = plan.fold_layout(10 * GiB + 512, [("a", 3 * GiB + 1), ("b", 1 << 20)])
    assert at == [("a", 10 * GiB + (1 << 20)), ("b", 13 * GiB + (2 << 20))]
    assert gb == 14


# ---------------- LVM text metadata ----------------

def test_lvm_parse_text():
    import lvmlayout
    t = '''vg1 {
id = "abc"
seqno = 3
extent_size = 8192	# 4 Megabytes
physical_volumes {
pv0 {
id = "pv-0"
device = "/dev/sdc1"	# Hint only
pe_start = 2048
pe_count = 10
}
}
logical_volumes {
lv {
status = ["READ", "WRITE", "VISIBLE"]
segment_count = 1
segment1 {
start_extent = 0
extent_count = 10
type = "striped"
stripe_count = 1	# linear
stripes = [
"pv0", 0
]
}
}
}
}
contents = "Text Format Volume Group"
'''
    d = lvmlayout.parse_text(t)
    vg = d["vg1"]
    assert vg["extent_size"] == 8192 and vg["physical_volumes"]["pv0"]["pe_start"] == 2048
    assert vg["logical_volumes"]["lv"]["segment1"]["stripes"] == ["pv0", 0]
    assert vg["logical_volumes"]["lv"]["status"] == ["READ", "WRITE", "VISIBLE"]
    assert lvmlayout.dashed("0123456789abcdefghijklmnopqrstuv") == "012345-6789-abcd-efgh-ijkl-mnop-qrstuv"


def test_plan_windows_spanned_volume_warns_and_never_folds():
    i = inv(guest_id="windows2019srv_64Guest",
            disks=[disk("scsi0:0", 60), disk("sata0:0", 8), disk("sata0:1", 8)],
            boot_disk="scsi0:0", boot_disk_source="guest C:\\",
            guest_mounts=[{"path": "C:\\", "disks": ["scsi0:0", "sata0:0"]},
                          {"path": "E:\\", "disks": ["sata0:0", "sata0:1"]},
                          {"path": "F:\\", "disks": []}])        # Tools cannot map a dynamic volume
    p = plan.build(i, "z")
    assert p["folds"] == [] and len(p["volumes_gb"]) == 2
    assert sum("Windows dynamic volume" in w for w in p["warnings"]) == 3


@needs_sgdisk
def test_windows_dynamic_disk_left_untouched(tmp_path, monkeypatch):
    img = str(tmp_path / "ldm.img")
    with open(img, "wb") as f:
        f.truncate(64 << 20)
    # what diskpart's "convert dynamic" leaves on a GPT disk: LDM metadata at LBA 34
    subprocess.run(["sgdisk", "-o", "-n", "1:34:2081", "-t", "1:5808C8AA-7E8F-42E0-85D2-E1E90434CFB3",
                    "-n", "2:2082:+16M", "-t", "2:E3C9E316-0B5C-4DB8-817D-F92DF00215AE",
                    "-n", "3:0:0", "-t", "3:AF9B60A0-1431-4F62-BC68-3311714A69AD", img],
                   check=True, capture_output=True)
    before = open(img, "rb").read()
    monkeypatch.setenv("V2V_TEST_LBS", "4096")
    r = guestfix.adapt(img)
    assert r["foreign"] == "Windows dynamic disk (LDM)" and r["action"] == "none" and not r["compatible"]
    assert any("dynamic disk" in p for p in r["problems"])
    assert open(img, "rb").read() == before


# ---------------- groups (0.3) ----------------

from exo_v2v import group as G  # noqa: E402


def nic(mac, pg, ips=(), origin="manual", label="Network adapter 1"):
    return {"label": label, "mac": mac, "port_group": pg, "connected": True,
            "ips": [{"ip": i, "prefix": 24, "origin": origin} for i in ips]}


NETS = {"front": {"source": "pg-front", "type": "public"},
        "app": {"source": "pg-app", "type": "private", "cidr": "10.20.0.0/24", "keep_addresses": True,
                "exoscale_name": "v2v-shop-app"}}


def test_network_map_private_static_gets_public_nic_first():
    nets, errs = plan.network_map([nic("00:50:56:00:00:31", "pg-app", ["10.20.0.31"])], NETS)
    assert not errs
    assert [n["network"] for n in nets] == ["public", "v2v-shop-app"]
    assert nets[0]["source_mac"] is None and nets[0]["mode"] == "dhcp"
    assert nets[1] == {"network": "v2v-shop-app", "name": "app", "source_mac": "00:50:56:00:00:31", "mode": "static",
                       "port_group": "pg-app", "ip": "10.20.0.31", "prefix": 24, "origin": "manual", "nogw": True}


def test_network_map_load_balancer_two_nics():
    nets, errs = plan.network_map([nic("00:50:56:00:00:01", "pg-front", ["192.168.10.50"], "dhcp"),
                                   nic("00:50:56:00:00:10", "pg-app", ["10.20.0.10"], label="Network adapter 2")], NETS)
    assert not errs
    assert nets[0]["source_mac"] == "00:50:56:00:00:01" and nets[1]["ip"] == "10.20.0.10"


def test_network_map_errors():
    _n, errs = plan.network_map([nic("00:50:56:00:00:09", "pg-other")], NETS)
    assert "not mapped" in errs[0]
    _n, errs = plan.network_map([nic("00:50:56:00:00:09", "pg-app")], NETS)        # Tools gave no address
    assert "address on app unknown" in errs[0]
    _n, errs = plan.network_map([nic("00:50:56:00:00:09", "pg-app", ["10.20.0.254"])], NETS)
    assert "keeps for its DHCP server" in errs[0]
    nets, errs = plan.network_map([nic("00:50:56:00:00:09", "pg-app")], NETS, {"app": "10.20.0.40"})
    assert not errs and nets[1]["ip"] == "10.20.0.40" and nets[1]["mode"] == "static"
    nets, errs = plan.network_map([nic("00:50:56:00:00:09", "pg-app", ["10.20.0.41"], "dhcp")], NETS)
    assert nets[1]["mode"] == "dhcp" and nets[1]["ip"] == "10.20.0.41"       # DHCP guest: the lease keeps it


def test_privnet_range():
    assert plan.privnet_range("10.20.0.0/24") == ("10.20.0.1", "10.20.0.253", "255.255.255.0")


def manifest(**kw):
    m = {"name": "shop", "zone": "ch-gva-2", "networks": {k: dict(v) for k, v in NETS.items()},
         "anti_affinity": {"app": ["app01", "app02"]},
         "vms": [{"vm": "db01", "tier": 1, "health": {"tcp": 5432}},
                 {"vm": "app01", "tier": 2}, {"vm": "app02", "tier": 2},
                 {"vm": "lb01", "tier": 3, "health": {"http": "http://{public_ip}/", "expect": "ok"}}],
         "cutover": {"max_window": "30m"}}
    m.update(kw)
    return m


def test_manifest_validation_and_defaults():
    m = G.validate(manifest(defaults={"receiver_template": "exo-v2v-linux-legacy"}))
    assert m["cutover"]["max_window"] == 1800 and m["cutover"]["health_timeout"] == 600
    assert m["vms"][1]["receiver_template"] == "exo-v2v-linux-legacy"
    assert m["networks"]["app"]["exoscale_name"] == "v2v-shop-app"
    for bad in (manifest(name="Shop!"), manifest(vms=[{"vm": "a", "tier": 0}]),
                manifest(vms=[{"vm": "a", "colour": "red"}]), manifest(anti_affinity={"x": ["nope"]}),
                manifest(networks={"n": {"source": "pg", "type": "private"}})):
        with pytest.raises(SystemExit):
            G.validate(bad)


def test_placeholders_hooks_and_health_ports():
    ctx = {"lb01": {"public_ip": "1.2.3.4", "private_ip": "10.20.0.10", "ip.app": "10.20.0.10"},
           "db01": {"public_ip": "5.6.7.8", "private_ip": "10.20.0.31"}}
    assert G.expand("dns {lb01.public_ip} db {db01.ip.app}{x}", ctx) == "dns 1.2.3.4 db {db01.ip.app}{x}"
    assert G.expand("http://{public_ip}:8080/", ctx, "db01") == "http://5.6.7.8:8080/"
    assert G.health_ports({"tcp": 5432}) == [5432]
    assert G.health_ports({"http": "https://{public_ip}/x"}) == [443]
    assert G.health_ports({"http": "http://{public_ip}:8080/h"}) == [8080]
    assert G.health_ports({"http": "http://{private_ip}/"}) == []
    assert G.duration("2h") == 7200 and G.parse_rule("tcp/80 0.0.0.0/0") == ("tcp", "80", "0.0.0.0/0")


def test_netfix_map_and_windows_static_script():
    m = [{"mac": "06:a1:02:00:07:cd", "mode": "dhcp"},
         {"mac": "0a:e2:d0:88:6e:d6", "mode": "static", "ip": "10.20.0.21", "prefix": 24, "nogw": True}]
    assert guestfix.netfix_map(m).splitlines()[1:] == ["06:a1:02:00:07:cd dhcp",
                                                       "0a:e2:d0:88:6e:d6 static 10.20.0.21/24 nogw"]
    ps1 = guestfix.win_static_ps1(m)
    assert 'Mac = "0A-E2-D0-88-6E-D6"; Ip = "10.20.0.21"; Len = 24' in ps1
    assert guestfix.win_static_ps1(m[:1]) is None and guestfix.netfix_map([]) is None
    assert "exo-v2v-static-ip" in guestfix.WIN_TASK_BAT.format(ps1="s.ps1", bat="b.bat", task="exo-v2v-static-ip")


def test_web_is_read_only_and_serves_state(tmp_path, monkeypatch):
    import json as J
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from exo_v2v import state, web
    monkeypatch.setenv("EXO_V2V_HOME", str(tmp_path))
    st = state.State.new("shop-db01", {"vm": "db01", "zone": "ch-gva-2", "disks": [{"capacity": GiB}],
                                       "networks": [{"network": "v2v-shop-app", "ip": "10.20.0.31"}]})
    st.data["group"] = "shop"
    st.data["cycles"].append({"t": 1, "seconds": 3, "disks": {"scsi0:0": {"covered": 5, "mode": "full"}}})
    st.save()
    gd = tmp_path / "groups" / "shop" / "logs"
    gd.mkdir(parents=True)
    (gd / "db01.log").write_text("line1\nline2\n")
    (gd.parent / "group.json").write_text(J.dumps({"name": "shop", "manifest": {"zone": "ch-gva-2"},
                                                   "members": [{"vm": "db01", "migration": "shop-db01", "tier": 1}]}))
    before = sorted(p.stat().st_mtime for p in tmp_path.rglob("*") if p.is_file())
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    get = lambda p: J.load(urllib.request.urlopen(base + p))
    try:
        o = get("/api/overview")
        assert o["migrations"][0]["private_ips"] == ["10.20.0.31"] and o["groups"][0]["members"][0]["vm"] == "db01"
        assert get("/api/migrations/shop-db01")["cycles"][0]["seconds"] == 3
        assert get("/api/groups/shop/log/db01")["lines"] == ["line1", "line2"]
        assert b"exo-v2v" in urllib.request.urlopen(base + "/").read()
        for path in ("/api/groups/shop/log/..%2F..%2Fshop-db01%2Fstate", "/api/migrations/..", "/api/groups/nope"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + path)
            assert e.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + "/api/overview", data=b"{}", method="POST"))
        assert e.value.code == 405
        web.Handler.token = "s3cret"
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/api/overview")
        assert e.value.code == 401
        req = urllib.request.Request(base + "/api/overview", headers={"Authorization": "Bearer s3cret"})
        assert J.load(urllib.request.urlopen(req))["migrations"]
    finally:
        web.Handler.token = None
        srv.shutdown()
    assert sorted(p.stat().st_mtime for p in tmp_path.rglob("*") if p.is_file()) == before


# ---------------- rehearsal (0.4) ----------------

from exo_v2v import rehearsal as RH  # noqa: E402


class FakeExo:
    def __init__(self, nets):
        self.nets = nets

    def privnet(self, name):
        return self.nets[name]


class FakeSt:
    def __init__(self, plan, name="shop-db01"):
        self.data = {"plan": plan}
        self.name = name

    def __getitem__(self, k):
        return self.data[k]


def test_rehearsal_layout_relay_address_and_dns_aliases(tmp_path, monkeypatch):
    monkeypatch.setenv("EXO_V2V_HOME", str(tmp_path))
    plan_ = {"group": "shop", "networks": [{"network": "public"},
                                           {"network": "v2v-shop-app", "ip": "10.20.0.31", "name": "app"}],
             "inventory": {"nics": [{"dns": ["10.20.0.1", "8.8.8.8", "fe80::1"]}]}}
    plan2 = {"group": "shop", "networks": [{"network": "public"},
                                           {"network": "v2v-shop-app", "ip": "10.20.0.253", "name": "app"}]}
    rh = RH.Rehearsal("shop")
    rh.data = {"settings": {**RH.DEFAULTS, "front_cidr": "100.127.0.0/24"}}
    ex = FakeExo({"v2v-shop-app": {"start_ip": "10.20.0.1", "netmask": "255.255.255.0"}})
    nets = RH.layout(rh, {"db01": FakeSt(plan_), "x": FakeSt(plan2, "shop-x")}, ex)
    assert [n["name"] for n in nets] == ["v2v-shop-rh-front", "v2v-shop-rh-app"]
    front, app = nets
    assert front["relay_ip"] == "100.127.0.253" and front["aliases"] == []
    assert app["relay_ip"] == "10.20.0.252"          # .253 is a member's address, .254 Exoscale's DHCP
    assert app["aliases"] == ["10.20.0.1"]           # the guests' on-prem DNS, answered by the sinkhole
    rh.data["settings"]["front_cidr"] = "10.20.0.0/16"
    with pytest.raises(SystemExit):
        RH.layout(rh, {"db01": FakeSt(plan_)}, ex)


def test_rehearsal_settings_and_relay_userdata(tmp_path):
    import argparse
    k = tmp_path / "k.pub"
    k.write_text("ssh-ed25519 AAAAuser alice\n")
    a = argparse.Namespace(access=["198.51.100.0/24"], ssh_key=[str(k)], ttl="2h", front_cidr=None,
                           host=["db.internal=10.20.0.31"])
    s = RH.settings_of({"access": ["203.0.113.7/32"], "hosts": {"a.internal": "10.20.0.21"}}, a)
    assert s["access"] == ["203.0.113.7/32", "198.51.100.0/24"] and s["ttl_s"] == 7200
    assert s["hosts"] == {"a.internal": "10.20.0.21", "db.internal": "10.20.0.31"}
    assert s["ssh_keys"] == ["ssh-ed25519 AAAAuser alice"] and s["front_cidr"] == "100.127.0.0/24"
    ud = RH.relay_userdata("ssh-ed25519 AAAAappliance", s["ssh_keys"])
    import yaml
    d = yaml.safe_load(ud)
    assert d["users"][1]["name"] == "rehearsal" and d["users"][1]["ssh_authorized_keys"] == s["ssh_keys"]
    files = {f["path"]: f["content"] for f in d["write_files"]}
    assert "net.ipv4.ip_forward = 0" in files["/etc/sysctl.d/90-v2v-relay.conf"]
    assert "policy drop" in files["/etc/nftables.conf"]
    sshd = files["/etc/ssh/sshd_config.d/90-v2v-relay.conf"]
    for line in ("PasswordAuthentication no", "PermitTunnel no", "GatewayPorts no", "AllowTcpForwarding local"):
        assert line in sshd
    assert files["/etc/ssh/sshd_config.d/91-v2v-permitopen.conf"].strip() == "PermitOpen none"
    assert "no-resolv" in files["/etc/dnsmasq.d/v2v-relay.conf"]


def test_relay_files_networkd_and_sinkhole():
    class Ex:
        def privnet(self, n):
            return {"id": {"v2v-s-rh-front": "f", "v2v-s-rh-app": "p"}[n]}

        def instance_nics(self, n):
            return [{"network": "public", "mac": "06:00:00:00:00:01"}, {"network": "f", "mac": "0a:00:00:00:00:02"},
                    {"network": "p", "mac": "0a:00:00:00:00:03"}]
    rh = RH.Rehearsal("s")
    rh.data = {"relay": {"name": "v2v-s-rh-relay"}, "settings": {"hosts": {"db.internal": "10.20.0.31"}},
               "networks": [{"name": "v2v-s-rh-front", "role": "front", "relay_ip": "100.127.0.253", "prefix": 24, "aliases": []},
                            {"name": "v2v-s-rh-app", "role": "private", "relay_ip": "10.20.0.252", "prefix": 24,
                             "aliases": ["10.20.0.1"]}]}
    f = RH.relay_files(rh, Ex())
    nw = [v for k, v in f.items() if k.startswith("/etc/systemd/network/")]
    assert len(nw) == 2 and all("Gateway" not in v for v in nw)           # the public NIC is left alone
    app = next(v for v in nw if "0a:00:00:00:00:03" in v)
    assert "Address=10.20.0.252/24" in app and "Address=10.20.0.1/24" in app
    dns = f["/etc/dnsmasq.d/v2v-relay.conf"]
    assert "listen-address=10.20.0.1" in dns and "address=/db.internal/10.20.0.31" in dns and "log-queries" in dns
    assert "server=" not in dns


def test_shadow_template_defaults():
    assert RH.shadow_template({"receiver_mode": "template", "template": "t", "template_visibility": "private"}) == ("t", "private")
    assert RH.shadow_template({"receiver_mode": "kexec", "os_family": "windows", "firmware": "efi"}) == \
        ("exo-v2v-windows-uefi", "private")
    assert RH.shadow_template({"receiver_mode": "kexec", "os_family": "linux", "firmware": "bios"}) == \
        ("exo-v2v-linux-legacy", "private")


def test_agent_skips_the_nocloud_drive(tmp_path, monkeypatch):
    import agent
    # as seen on a private instance: the NoCloud drive is vdb, 1 MiB iso9660, NOT flagged read-only
    sysb = {"vda": ("0", 83886080, ""), "vdb": ("0", 2048, ""), "vdc": ("0", 23068672, "5e8559a9-ce53-4ef6-b")}
    def fake_open(path, *a, **k):
        import io
        dev, attr = path.split("/")[3], path.split("/")[-1]
        ro, size, serial = sysb[dev]
        return io.StringIO({"ro": ro, "size": str(size), "serial": serial}[attr])
    monkeypatch.setattr(agent.glob, "glob", lambda p: [f"/sys/block/{d}" for d in sysb] if p == "/sys/block/vd*" else [])
    monkeypatch.setattr(agent, "open", fake_open, raising=False)
    monkeypatch.setattr(agent.os.path, "exists", lambda p: p.endswith("/serial"))
    assert agent.disks() == ["/dev/vda", "/dev/vdc"]
    assert agent.root_disk() == "/dev/vda"


def test_migration_names(tmp_path, monkeypatch):
    from exo_v2v import state
    monkeypatch.setenv("EXO_V2V_HOME", str(tmp_path))
    for bad in ("groups", "rehearsals", "jobs", "../x", "a/b", "", ".hidden", "x" * 70):
        with pytest.raises(state.BadName):
            state.State.new(bad, {})
    assert state.State.new("web01.prod_A-2", {}).name == "web01.prod_A-2"


def test_vsphere_keepalive():
    import threading
    import time
    from exo_v2v import vsphere
    calls = []

    class SI:
        def CurrentTime(self):
            calls.append(1)
            if len(calls) == 2:
                raise ConnectionError("blip")      # logged, the thread goes on

    vc = vsphere.VC.__new__(vsphere.VC)
    vc.si, vc._stop = SI(), threading.Event()
    t = threading.Thread(target=vc._keepalive, args=(0.01,), daemon=True)
    t.start()
    time.sleep(0.2)
    vc._stop.set()
    t.join(1)
    assert len(calls) >= 3 and not t.is_alive()


def test_lock_fd_is_shared_with_a_child(tmp_path, monkeypatch):
    """--lock-fd: a group process hands its migration lock to the member's
    process; the lock stays held while either lives, never released by the child."""
    import fcntl
    import json
    from exo_v2v import state
    monkeypatch.setenv("EXO_V2V_HOME", str(tmp_path))
    st = state.State.new("m1", {})
    lk = open(os.path.join(st.dir, ".lock"), "a+")
    fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    lk.write(json.dumps({"pid": os.getpid(), "what": "consistent point"}))
    lk.flush()
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    code = ("import sys, time; sys.path.insert(0, sys.argv[1]); from exo_v2v import cli, state\n"
            "st = state.State.load('m1'); cli.migration_lock(st, 'sync', int(sys.argv[2]))\n"
            "print('held', flush=True); time.sleep(1.5)")
    child = subprocess.Popen([sys.executable, "-c", code, src, str(lk.fileno())], pass_fds=(lk.fileno(),),
                             stdout=subprocess.PIPE, text=True, env=dict(os.environ, EXO_V2V_HOME=str(tmp_path)))
    assert child.stdout.readline().strip() == "held"
    lk.close()                                        # the group process lets go: the child still holds it
    other = open(os.path.join(st.dir, ".lock"), "a+")
    with pytest.raises(BlockingIOError):
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert child.wait() == 0
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)  # both gone: free
    assert json.load(open(os.path.join(st.dir, ".lock")))["what"] == "consistent point"   # holder kept
    other.close()
    wrong = open(tmp_path / "other", "w")
    r = subprocess.run([sys.executable, "-c", code, src, str(wrong.fileno())], pass_fds=(wrong.fileno(),),
                       capture_output=True, text=True, env=dict(os.environ, EXO_V2V_HOME=str(tmp_path)))
    assert r.returncode and "is not the lock of m1" in r.stderr
