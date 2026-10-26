"""vCenter code paths against govmomi's simulator (vcsim): VM lookup by name,
path and UUID, folders, tags, the appliance checks, `group discover`.

Skipped unless pyVmomi is installed and a `vcsim` binary is found ($VCSIM or
PATH). tests/vcsim/sitecustomize.py works around two simulator quirks.

Run: VCSIM=/path/to/vcsim python3 -m pytest -q tests/test_vcsim.py
"""
import base64
import importlib.util
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.request

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
SHIM = os.path.join(HERE, "vcsim")
sys.path.insert(0, SRC)

pyVmomi = pytest.importorskip("pyVmomi")
VCSIM = os.environ.get("VCSIM") or shutil.which("vcsim")
if not VCSIM:
    pytest.skip("no vcsim binary ($VCSIM or PATH)", allow_module_level=True)

_spec = importlib.util.spec_from_file_location("vcsim_shim", os.path.join(SHIM, "sitecustomize.py"))
_spec.loader.exec_module(importlib.util.module_from_spec(_spec))

from pyVmomi import vim  # noqa: E402
from exo_v2v import group, vsphere as V  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rest(port, method, path, body=None, sid=None):
    req = urllib.request.Request(f"https://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if sid:
        req.add_header("vmware-api-session-id", sid)
    else:
        req.add_header("Authorization", "Basic " + base64.b64encode(b"u:p").decode())
    with urllib.request.urlopen(req, context=ssl._create_unverified_context()) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def wait(task):
    from pyVim.task import WaitForTask
    WaitForTask(task)


@pytest.fixture(scope="module")
def sim():
    """DC0: shop/web01, shop/db/db01; DC1: web01 (same name), a template named db01.
    Tag app:shop on both DC0 VMs."""
    port = free_port()
    proc = subprocess.Popen([VCSIM, "-l", f"127.0.0.1:{port}", "-dc", "2", "-cluster", "1", "-host", "2",
                             "-vm", "2", "-standalone-host", "0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    vc = V.VC(f"127.0.0.1:{port}", "u", "p")
    si = vc.content.searchIndex
    vm = {p: si.FindByInventoryPath(p) for p in ("DC0/vm/DC0_C0_RP0_VM0", "DC0/vm/DC0_C0_RP0_VM1",
                                                   "DC1/vm/DC1_C0_RP0_VM0", "DC1/vm/DC1_C0_RP0_VM1")}
    dc0 = si.FindByInventoryPath("DC0/vm")
    shop = dc0.CreateFolder("shop")
    db = shop.CreateFolder("db")
    wait(shop.MoveIntoFolder_Task([vm["DC0/vm/DC0_C0_RP0_VM0"]]))
    wait(db.MoveIntoFolder_Task([vm["DC0/vm/DC0_C0_RP0_VM1"]]))
    for p, name in (("DC0/vm/DC0_C0_RP0_VM0", "web01"), ("DC0/vm/DC0_C0_RP0_VM1", "db01"),
                    ("DC1/vm/DC1_C0_RP0_VM0", "web01"), ("DC1/vm/DC1_C0_RP0_VM1", "db01")):
        wait(vm[p].Rename_Task(name))
    wait(vm["DC1/vm/DC1_C0_RP0_VM1"].PowerOffVM_Task())
    vm["DC1/vm/DC1_C0_RP0_VM1"].MarkAsTemplate()
    sid = rest(port, "POST", "/rest/com/vmware/cis/session")["value"]
    cat = rest(port, "POST", "/rest/com/vmware/cis/tagging/category", {"create_spec": {
        "name": "app", "description": "", "cardinality": "SINGLE", "associable_types": ["VirtualMachine"]}}, sid)["value"]
    tag = rest(port, "POST", "/rest/com/vmware/cis/tagging/tag", {"create_spec": {
        "name": "shop", "description": "", "category_id": cat}}, sid)["value"]
    for p in ("DC0/vm/DC0_C0_RP0_VM0", "DC0/vm/DC0_C0_RP0_VM1"):
        rest(port, "POST", f"/rest/com/vmware/cis/tagging/tag-association/id:{tag}?~action=attach",
             {"object_id": {"id": vm[p]._moId, "type": "VirtualMachine"}}, sid)
    yield {"port": port, "vc": vc, "web0": vm["DC0/vm/DC0_C0_RP0_VM0"], "db0": vm["DC0/vm/DC0_C0_RP0_VM1"],
           "web1": vm["DC1/vm/DC1_C0_RP0_VM0"]}
    vc.close()
    proc.terminate()
    proc.wait()


def test_vm_by_name_path_and_uuid(sim):
    vc = sim["vc"]
    assert vc.vm("db01") == sim["db0"]                       # the DC1 template of that name does not count
    with pytest.raises(SystemExit) as e:
        vc.vm("web01")
    assert "DC0/vm/shop/web01" in str(e.value) and "DC1/vm/web01" in str(e.value)
    assert vc.vm("DC1/vm/web01") == sim["web1"]
    assert vc.vm("/DC0/vm/shop/db/db01") == sim["db0"]
    with pytest.raises(SystemExit):
        vc.vm("DC0/vm/shop")                                 # a folder, not a VM
    inv = V.inventory(vc, sim["web0"])
    assert inv["path"] == "DC0/vm/shop/web01" and inv["server"]["vcenter"] and inv["datastores"]
    assert vc.vm("uuid:" + inv["instance_uuid"]) == sim["web0"]
    # a plan finds its VM by instance UUID after a rename and a folder move
    wait(sim["web0"].Rename_Task("web01-renamed"))
    try:
        assert vc.vm_of({"vm": "web01", "inventory": inv}) == sim["web0"]
    finally:
        wait(sim["web0"].Rename_Task("web01"))


def test_folders_and_tags(sim):
    vc = sim["vc"]

    def names(vms):
        return sorted(vc.path(v) for v in vms)
    assert names(vc.folder_vms("DC0/vm/shop")) == ["DC0/vm/shop/db/db01", "DC0/vm/shop/web01"]
    assert names(group.discover_vms(vc, tags=["app:shop"])) == ["DC0/vm/shop/db/db01", "DC0/vm/shop/web01"]
    both = group.discover_vms(vc, names=["DC1/vm/web01"], folders=["DC0/vm/shop"], tags=["app:shop"])
    assert names(both) == ["DC0/vm/shop/db/db01", "DC0/vm/shop/web01", "DC1/vm/web01"]
    with pytest.raises(SystemExit):
        group.discover_vms(vc, tags=["app:nope"])


def test_appliance_checks(sim):
    vc = sim["vc"]
    errs, _ = V.appliance_checks(vc, sim["web0"], sim["web1"])  # appliance in the other datacenter
    assert errs and "does not mount datastore" in errs[0]
    assert V.appliance_checks(vc, sim["web0"], sim["db0"]) == ([], [])
    assert V.missing_privileges(vc, sim["web0"], sim["db0"]) == []
    cluster = sim["db0"].runtime.host.parent
    spec = vim.cluster.ConfigSpecEx(drsConfig=vim.cluster.DrsConfigInfo(
        enabled=True, defaultVmBehavior=vim.cluster.DrsConfigInfo.DrsBehavior.fullyAutomated))
    wait(cluster.ReconfigureComputeResource_Task(spec, True))
    _, warns = V.appliance_checks(vc, sim["web0"], sim["db0"])
    assert warns and "DRS" in warns[0]
    over = vim.cluster.ConfigSpecEx(drsVmConfigSpec=[vim.cluster.DrsVmConfigSpec(
        operation="add", info=vim.cluster.DrsVmConfigInfo(key=sim["db0"], enabled=False))])
    wait(cluster.ReconfigureComputeResource_Task(over, True))
    assert V.appliance_checks(vc, sim["web0"], sim["db0"]) == ([], [])


def test_group_discover_writes_paths_for_ambiguous_names(sim, tmp_path, capsys):
    out = tmp_path / "g1.yaml"
    env = dict(os.environ, VSPHERE_HOST=f"127.0.0.1:{sim['port']}", VSPHERE_USER="u", VSPHERE_PASSWORD="p",
               EXO_V2V_HOME=str(tmp_path / "home"), PYTHONPATH=os.pathsep.join([SHIM, SRC]))
    r = subprocess.run([sys.executable, "-m", "exo_v2v", "group", "discover", "g1", "--tag", "app:shop",
                        "--zone", "ch-gva-2", "-o", str(out)], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    vms = [ln.split("vm:", 1)[1].strip() for ln in out.read_text().splitlines() if ln.strip().startswith("- vm:")]
    assert sorted(vms) == ["DC0/vm/shop/web01", "db01"]       # web01 is ambiguous: its path
    assert group.member_name("g1", "DC0/vm/shop/web01") == "g1-web01"
    m = out.read_text().replace("vms:", "vms:\n  - vm: DC1/vm/web01\n    tier: 1", 1)
    (tmp_path / "g2.yaml").write_text(m)
    with pytest.raises(SystemExit):
        group.load_manifest(str(tmp_path / "g2.yaml"))
    assert "same name" in capsys.readouterr().err
