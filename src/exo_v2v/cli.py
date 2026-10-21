"""exo-v2v — warm (online) migration of VMware vSphere VMs to Exoscale.

Workflow (each step is resumable; state in ~/.exo-v2v/<name>/):

  exo-v2v plan     NAME --vm VMNAME --zone ZONE [--instance-type T] [--security-group SG ...]
  exo-v2v create   NAME                 # receiver instance + volumes, RAM-boots the agent
  exo-v2v sync     NAME [--full] [--every SECONDS] [--cycles N]
  exo-v2v cutover  NAME [--verify]      # final cycle (VM powered off), convert, start on Exoscale
  exo-v2v status   [NAME]
  exo-v2v rollback NAME                 # power the source VM back on; receiver stopped
  exo-v2v cleanup  NAME [--delete-rollback-volumes] [--delete-instance]

vSphere connection: VSPHERE_HOST, VSPHERE_USER, VSPHERE_PASSWORD (env) or
--vsphere-host/--vsphere-user; vCenter or standalone ESXi. Run sync/cutover as
root inside the replication appliance VM (hot-add transport).
Exoscale: the `exo` CLI's configured account (exo config) or --account.
"""
import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

from . import __version__
from . import plan as planmod
from . import userdata
from .exo import Exo, ExoError, volume_size_gb
from .state import State
from .tunnel import Tunnel
from .util import die, gib_ceil, human, log

LABEL = "exo-v2v"


# ---------------- helpers ----------------

def vsphere_connect(a):
    from . import vsphere as V
    host = a.vsphere_host or os.environ.get("VSPHERE_HOST")
    user = a.vsphere_user or os.environ.get("VSPHERE_USER")
    pw = os.environ.get("VSPHERE_PASSWORD")
    if not (host and user and pw):
        die("vSphere connection: set VSPHERE_HOST, VSPHERE_USER and VSPHERE_PASSWORD")
    return V, V.VC(host, user, pw, insecure=not a.vsphere_verify_tls)


def exo_for(st, a):
    return Exo(st["plan"]["zone"], account=a.account)


def sender_cidr(a):
    if a.sender_cidr:
        return a.sender_cidr
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            ip = urllib.request.urlopen(url, timeout=10).read().decode().strip()
            if ip.count(".") == 3:
                return f"{ip}/32"
        except OSError:
            continue
    die("cannot detect this host's public IP: pass --sender-cidr A.B.C.D/32")


def tunnel_for(st):
    r = st["receiver"]
    return Tunnel(r["ip"], st["plan"]["receiver_user"], st.key_path, os.path.join(st.dir, "known_hosts"))


def mapping_of(st):
    return {d["disk"]: d["export"] for d in st["plan"]["disks"]}


def print_plan(p):
    print(f"VM {p['vm']}: {p['os_family']} ({p['guest_id']}), firmware {p['firmware']}")
    print(f"  receiver: {p['template']} as {p['convert_type']} -> final {p['target_type']} in {p['zone']}")
    for d in p["disks"]:
        where = (f"inside the root disk at {human(d['offset'])}" if d["role"] == "fold"
                 else f"{d['size_gb']} GB")
        print(f"  {d['disk']:<9} {d['label']:<14} {human(d['capacity']):>10} -> {d['role']:<6} "
              f"{where} (export {d['export']})")
    for k in ("errors", "warnings", "notes"):
        for m in p[k]:
            print(f"  {k[:-1].upper()}: {m}")


# ---------------- commands ----------------

def cmd_plan(a):
    V, vc = vsphere_connect(a)
    try:
        inv = V.inventory(vc, vc.vm(a.vm))
    finally:
        vc.close()
    boot_mode = None
    if a.receiver_template:
        try:
            boot_mode = Exo(a.zone, a.account).template_boot_mode(
                a.receiver_template, a.receiver_template_visibility)
        except ExoError as e:
            die(f"receiver template {a.receiver_template!r} ({a.receiver_template_visibility}): {e}")
    p = planmod.build(inv, a.zone, a.instance_type, a.system_disk, a.security_group or [],
                      a.receiver_template, a.receiver_template_visibility, boot_mode,
                      fold=a.fold or [], auto_fold=not a.no_fold)
    p["inventory"] = inv
    print_plan(p)
    if p["errors"] and not a.force:
        die("plan has errors (fix them, or --force to record the plan anyway)")
    st = State.new(a.name, p)
    log(f"plan saved: {st.path}")


def cmd_create(a):
    st = State.load(a.name)
    p = st["plan"]
    if p["errors"]:
        die("plan has errors")
    ex = exo_for(st, a)
    if st["receiver"].get("id") and ex.instance_exists(st["receiver"]["name"]):
        die(f"receiver {st['receiver']['name']} already exists")
    if not os.path.exists(st.key_path):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"exo-v2v-{a.name}",
                        "-f", st.key_path], check=True)
    name = a.instance_name or f"v2v-{a.name}"
    labels = {LABEL: a.name}
    sg = f"exo-v2v-{a.name}"
    cidr = sender_cidr(a)
    st.data["receiver"] = {"name": name, "sg": sg, "sender_cidr": cidr, "volumes": []}
    st.set_phase("receiver-creating")
    try:
        ex.sg(sg)
    except ExoError:
        ex.sg_create(sg)
        ex.sg_rule_add(sg, cidr, 22, desc="exo-v2v sender (SSH tunnel)")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(userdata.build(open(st.key_path + ".pub").read()))
        ud = f.name
    try:
        extra = "".join(", " + f for f, on in (("Secure Boot", p.get("secure_boot")), ("TPM", p.get("tpm"))) if on)
        log(f"creating {name} ({p['template']}, {p['convert_type']}, {p['root_gb']} GB{extra})")
        inst = ex.instance_create(name, p["template"], p["convert_type"], p["root_gb"], [sg], ud, labels,
                                  secureboot=p.get("secure_boot", False), tpm=p.get("tpm", False),
                                  template_visibility=p.get("template_visibility", "public"))
    finally:
        os.unlink(ud)
    st.data["receiver"].update(id=inst.get("id"), ip=inst.get("ip_address"))
    st.save()
    template_mode = p.get("receiver_mode") == "template"
    if template_mode:
        # attach volumes only once the receiver runs: a disk hot-plugged while
        # the firmware/GRUB still loads the RAM root can hang the boot
        wait_ssh(tunnel_for(st), a.timeout)
        log("receiver template booted into RAM")
    for i, d in enumerate((d for d in p["disks"] if d["role"] == "volume"), 1):
        vname = f"v2v-{a.name}-d{i}"   # `v2v-` prefix: the IAM template scopes volumes by name
        add_volume(st, ex, vname, d["size_gb"], d["export"])
    install_agent(st, 1 + len(st["receiver"]["volumes"]), timeout=a.timeout, template_mode=template_mode)
    if not template_mode:
        log("receiver installs its packages and kexecs into RAM (5-15 min)")
    wait_receiver(st, timeout=a.timeout)
    if p["os_family"] == "windows":
        with tunnel_for(st) as t:
            win_support(t)          # staged now: not part of the cutover downtime
    st.set_phase("receiver-ready")
    print(f"receiver {name} ready at {st['receiver']['ip']}")


def wait_ssh(t, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if t.ssh("true", timeout=30).returncode == 0:
            return
        time.sleep(10)
    die("receiver: SSH never came up (check the instance console)")


def install_agent(st, expected_disks, timeout=1800, template_mode=False):
    """Wait for SSH on the fresh instance, stream the agent payload, start the
    bootstrap (detached: it ends with kexec) - or, on a receiver template, which
    already runs from RAM, start the agent."""
    t = tunnel_for(st)
    wait_ssh(t, timeout)
    cmd = userdata.agent_start_command(expected_disks) if template_mode else userdata.bootstrap_command(expected_disks)
    payload = userdata.payload_tgz(st["plan"].get("folds", []),
                                   {v["export"]: v["serial"] for v in st["receiver"]["volumes"] if v.get("serial")})
    r = subprocess.run(t.ssh_base() + [f"{t.user}@{t.host}", "sudo sh -c " + repr(cmd)],
                       input=payload, capture_output=True, timeout=300)
    if r.returncode:
        die(f"receiver: agent install failed: {r.stderr.decode(errors='replace')[-400:]}")
    chk = t.ssh("test -s /opt/v2v-agent/agent.py && test -s /opt/v2v-agent/bootstrap-kexec.sh", timeout=60)
    if chk.returncode:
        die("receiver: agent payload missing after install")
    log("agent payload installed; " + ("agent started" if template_mode else "bootstrap started"))


def win_support(t):
    """virtio-win + rhsrvany on the receiver's RAM root (idempotent)."""
    if t.ssh("test -s /usr/share/virtio-win/virtio-win.iso && test -s /usr/share/virt-tools/rhsrvany.exe").returncode == 0:
        return
    log("Windows guest: staging virtio-win + rhsrvany on the receiver")
    script = "mount -o remount,size=85% / 2>/dev/null || true\n" + \
        open(os.path.join(userdata.RECEIVER_DIR, "win-support.sh")).read()
    out = t.ssh("sudo bash -s", input=script)
    if "WIN_SUPPORT_OK" not in out.stdout:
        die(f"win-support failed: {(out.stderr or out.stdout)[-500:]}")


def add_volume(st, ex, vname, gb, export):
    """Create + attach a Block Storage volume for `export` and record it."""
    ex.volume_create(vname, gb, {LABEL: st.name})
    ex.volume_attach(vname, st["receiver"]["name"])
    serial = ex.volume(vname)["id"][:20]          # what the instance sees as the virtio serial
    st.data["receiver"]["volumes"].append({"name": vname, "export": export, "serial": serial})
    st.save()
    log(f"volume {vname} {gb} GB attached")
    return serial


def wait_device(t, serial, present=True, timeout=300):
    """The receiver's device for a volume serial (or None once it is gone)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        dev = next((d["dev"] for d in t.get("/status").get("devices", []) if d["serial"] == serial), None)
        if bool(dev) == present:
            return dev
        time.sleep(3)
    die(f"volume {serial} {'never appeared on' if present else 'still attached to'} the receiver")


def wait_receiver(st, timeout=1800):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with tunnel_for(st) as t:
                s = t.get("/status", timeout=10)
                if s.get("ramroot"):
                    sizes = {d["export"]: d["size"] for d in s["disks"]}
                    for d in st["plan"]["disks"]:
                        if sizes.get(d["export"], 0) < d["capacity"]:
                            die(f"receiver {d['export']} is {sizes.get(d['export'])} B < source {d['capacity']} B")
                    st.data["receiver"]["agent_started"] = time.time() - s.get("uptime_s", 0)
                    st.save()
                    return s
        except (ConnectionError, OSError):
            pass
        time.sleep(20)
    die(f"receiver not ready after {timeout}s (check the console in the Exoscale portal; "
        "/var/log/v2v-bootstrap.log on the instance)")


def check_receiver_continuity(st, status):
    """A receiver that restarted lost its RAM state (/meta): the next cycle must
    be a full copy - vsync does that automatically when /meta is empty."""
    started = time.time() - status.get("uptime_s", 0)
    prev = st["receiver"].get("agent_started")
    if prev and started - prev > 120:
        log("receiver agent restarted since the last cycle: next cycle is a full copy")
    st.data["receiver"]["agent_started"] = started


def reconcile_disks(st, a, ex, V, vm):
    """Follow the source VM's disks between cycles: a grown Block Storage disk
    is resized (Exoscale: detached), a new disk gets a new volume, a grown
    system (or folded) disk needs a bigger root disk: the receiver is reset with
    it, keeping its volumes. Returns True when something changed."""
    p, r = st["plan"], st["receiver"]
    if r.get("rebuild_pending"):
        log("resuming the interrupted receiver reset")
        rebuild_receiver(st, a, ex)
    cur = V.disks_of(vm.config.hardware)
    known = {d["disk"]: d for d in p["disks"]}
    gone = sorted(set(known) - set(cur))
    if gone:
        die(f"disk(s) {', '.join(gone)} removed from {vm.name} since the plan: the copy no longer matches "
            "the VM (cleanup --delete-instance, then plan + create again)")
    grown = {k: cur[k].capacityInBytes for k in known if cur[k].capacityInBytes > known[k]["capacity"]}
    added = sorted(set(cur) - set(known), key=planmod.disk_key)
    detached = [v for v in r["volumes"] if ex.volume(v["name"]).get("state") != "attached"]
    if not grown and not added and not detached:
        return False
    with tunnel_for(st) as t:
        for v in detached:                     # an interrupted resize left it detached
            log(f"volume {v['name']} is detached: attaching it again")
            ex.volume_attach(v["name"], r["name"])
            serial = v.get("serial") or ex.volume(v["name"])["id"][:20]
            t.post("/exports", {v["export"]: wait_device(t, serial)})
        for k in added:
            d = cur[k]
            mode = getattr(d.backing, "diskMode", "")
            if mode.startswith("independent") or not str(getattr(d.backing, "fileName", "")).endswith(".vmdk"):
                die(f"new disk {k} ({mode or 'not a VMDK'}) cannot be replicated")
            nvol = sum(1 for x in p["disks"] if x["role"] == "volume")
            if nvol >= planmod.MAX_VOLUMES:
                die(f"new disk {k}: the instance already has {nvol} Block Storage volumes (the maximum)")
            used = {x["export"] for x in p["disks"]}
            export = next("sd" + c for c in "bcdefghijklmnopqrstuvwxyz" if "sd" + c not in used)
            n = 1 + max([int(v["name"].rsplit("-d", 1)[1].split("-")[0]) for v in r["volumes"]] or [0])
            gb = max(planmod.MIN_DISK_GB, gib_ceil(d.capacityInBytes + planmod.MiB))
            log(f"new disk {k} ({human(d.capacityInBytes)}) on {vm.name}: adding volume as {export}")
            serial = add_volume(st, ex, f"v2v-{st.name}-d{n}", gb, export)
            t.post("/exports", {export: wait_device(t, serial)})
            p["disks"].append({"disk": k, "export": export, "capacity": d.capacityInBytes, "label": d.deviceInfo.label,
                               "role": "volume", "size_gb": gb})
            p["volumes_gb"].append(gb)
            st.event(f"disk {k} added as {export} (full copy next cycle)")
        root_change = False
        for k, cap in grown.items():
            e = known[k]
            log(f"disk {k} grew {human(e['capacity'])} -> {human(cap)}")
            if e["role"] == "volume":
                vol = next(v for v in r["volumes"] if v["export"] == e["export"])
                gb = max(planmod.MIN_DISK_GB, gib_ceil(cap + planmod.MiB))
                if gb > volume_size_gb(ex.volume(vol["name"])):
                    serial = vol.get("serial") or ex.volume(vol["name"])["id"][:20]
                    log(f"resizing {vol['name']} to {gb} GB (detach, resize, attach)")
                    ex.volume_detach(vol["name"])
                    wait_device(t, serial, present=False)
                    ex.volume_resize(vol["name"], gb)
                    ex.volume_attach(vol["name"], r["name"])
                    t.post("/exports", {e["export"]: wait_device(t, serial)})
                    e["size_gb"] = gb
                    p["volumes_gb"] = [x["size_gb"] for x in p["disks"] if x["role"] == "volume"]
            else:
                root_change = True
            e["capacity"] = cap
            st.event(f"disk {k} grew to {cap} B (full copy next cycle)")
        if root_change:
            root = p["disks"][0]
            fold_at, root_gb = planmod.fold_layout(root["capacity"], [(x["disk"], x["capacity"])
                                                                      for x in p["disks"] if x["role"] == "fold"])
            moved = any(x["offset"] != dict(fold_at)[x["disk"]] for x in p["disks"] if x["role"] == "fold")
            if root_gb > p["root_gb"] or moved:
                p["folds"] = []
                for x in p["disks"]:
                    if x["role"] == "fold":
                        x["offset"] = dict(fold_at)[x["disk"]]
                        p["folds"].append({"export": x["export"], "disk": x["disk"], "offset": x["offset"],
                                           "size": x["capacity"]})
                root["size_gb"] = p["root_gb"] = root_gb
                r["rebuild_pending"] = True          # resumable: the next sync/cutover finishes it
                st.save()
                t.stop()
                rebuild_receiver(st, a, ex)
            else:
                st.save()
    st.save()
    return True


def rebuild_receiver(st, a, ex):
    """Reset the receiver's root disk (bigger, re-imaged from the template) and
    restart the agent; the Block Storage volumes are detached first and keep
    their copy (their change IDs are restored), the root disk is copied again."""
    p, r = st["plan"], st["receiver"]
    log(f"the root disk must grow to {p['root_gb']} GB: resetting {r['name']} (its volumes are kept; "
        "the system disk is copied again in the next cycle)")
    if "saved_meta" not in r:
        with tunnel_for(st) as t:
            r["saved_meta"] = {v["export"]: t.get(f"/meta?export={v['export']}") for v in r["volumes"]}
        st.save()
    for v in r["volumes"]:
        if ex.volume(v["name"]).get("state") == "attached":
            ex.volume_detach(v["name"])
    if ex.instance(r["name"]).get("state") != "stopped":
        ex.instance_stop(r["name"])                    # Exoscale resets stopped instances only
    ex.instance_reset(r["name"], p["template"], p["root_gb"], p.get("template_visibility", "public"))
    if ex.instance(r["name"]).get("state") != "running":
        ex.instance_start(r["name"])
    kh = os.path.join(st.dir, "known_hosts")
    if os.path.exists(kh):
        os.remove(kh)                                  # the re-imaged receiver has new host keys
    template_mode = p.get("receiver_mode") == "template"
    if template_mode:
        wait_ssh(tunnel_for(st), a.timeout)
    for v in r["volumes"]:
        if ex.volume(v["name"]).get("state") != "attached":
            ex.volume_attach(v["name"], r["name"])
    install_agent(st, 1 + len(r["volumes"]), timeout=a.timeout, template_mode=template_mode)
    wait_receiver(st, timeout=a.timeout)
    with tunnel_for(st) as t:
        if p["os_family"] == "windows":
            win_support(t)
        for export, m in r["saved_meta"].items():
            if m.get("change_id"):
                t.post(f"/meta?export={export}", m)
    r.pop("saved_meta"); r.pop("rebuild_pending", None)
    st.event(f"receiver reset with a {p['root_gb']} GB root disk")
    st.save()
    log("receiver ready again")


def run_cycle(st, a, V, vc, vm, proxy, t, full=False, final=False):
    status = t.get("/status")
    if not status.get("ramroot"):
        die("receiver is not in its RAM root any more (it rebooted from disk?): recreate it (exo-v2v cleanup --delete-instance; create)")
    check_receiver_continuity(st, status)
    cyc = V.sync(vc, vm, mapping_of(st), t.nbd, t.api, full=full, final=final,
                 shutdown_timeout=a.shutdown_timeout, workers=a.workers, transport=a.transport, proxy=proxy)
    cyc["t"] = time.time()
    st.data["cycles"].append(cyc)
    st.save()
    return cyc


def migration_lock(st):
    """One sync/cutover per migration at a time (the sweep and the receiver's
    changeId assume a single writer)."""
    import fcntl
    lk = open(os.path.join(st.dir, ".lock"), "w")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        die(f"another exo-v2v sync/cutover of {st.name} is running")
    st._lock = lk


def sender_context(a, st):
    migration_lock(st)
    V, vc = vsphere_connect(a)
    vm = vc.vm(st["plan"]["vm"])
    proxy = None
    if a.transport == "hotadd":
        if os.geteuid() != 0:
            die("hot-add transport: run as root inside the replication appliance VM")
        proxy = vc.vm(a.proxy_vm) if a.proxy_vm else V.self_vm(vc)
        if proxy is None:
            die("cannot identify the appliance VM by SMBIOS UUID: pass --proxy-vm NAME")
    for msg in V.sweep(vc, vm, proxy):
        log(f"cleanup: {msg}")
    if not vm.config.changeTrackingEnabled or not all(V.disk_ctk(vm).values()):
        log("enabling CBT on the source VM (VM + every disk)")
        log(f"CBT: {V.enable_cbt(vc, vm)}")
    return V, vc, vm, proxy


def cmd_sync(a):
    st = State.load(a.name)
    if st["phase"] not in ("receiver-ready", "syncing"):
        die(f"phase is {st['phase']}: sync needs a ready receiver")
    V, vc, vm, proxy = sender_context(a, st)
    ex = exo_for(st, a)
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True) or log("stopping after this cycle"))
    try:
        st.set_phase("syncing")
        n = 0
        while True:
            changed = reconcile_disks(st, a, ex, V, vm)
            with tunnel_for(st) as t:
                cyc = run_cycle(st, a, V, vc, vm, proxy, t, full=a.full and n == 0)
                n += 1
                log(f"cycle {len(st['cycles'])}: {sum(d['covered'] for d in cyc['disks'].values()) / 2**20:.0f} MiB "
                    f"in {cyc['seconds']}s")
                if n == 1 or changed:
                    preflight_report(st, t)
            if not a.every or stop["now"] or (a.cycles and n >= a.cycles):
                break
            for _ in range(a.every):
                if stop["now"]:
                    break
                time.sleep(1)
            if stop["now"]:
                break
    finally:
        vc.close()


def preflight_report(st, t):
    """Early warning (sync time, not cutover time) about volumes whose
    filesystems cannot be read on 4Kn Block Storage as they are."""
    try:
        pf = t.get("/preflight", timeout=120)
    except OSError:
        return
    st.data["preflight"] = pf
    st.save()
    for export, r in pf.get("disks", {}).items():
        for prob in r["problems"]:
            if prob.endswith(": rebuild"):
                how = "rebuilt onto new 4K volume(s) at cutover, LVM UUIDs kept (adds time proportional to its data)"
            elif "Windows dynamic disk" in prob or "Storage Spaces" in prob:
                how = "NOT handled: the volume stays unreadable on Exoscale (convert to basic disks first)"
            elif "fold the volumes" in prob:
                how = ("NOT handled: the volume group spans the system disk; plan again with VMware Tools "
                       "running, or with --fold DISK")
            elif " ntfs " in f" {prob} ":
                how = "boot sector rewritten for 4K sectors at cutover (in place, no copy)"
            elif any(t in prob for t in (" xfs ", " ext ")):
                how = "rebuilt onto a 4K volume at cutover (adds time proportional to its data)"
            elif "not 4 KiB aligned" in prob:
                how = "NOT handled automatically (unaligned partition): see docs/OPERATIONS.md"
            else:
                how = "NOT handled automatically: see docs/OPERATIONS.md"
            log(f"preflight {export}: {prob} -> {how}")


def relayout_group(st, ex, t, exports):
    """Rebuild the 512-only filesystems of `exports` (data volumes that belong
    together: one volume, or the volumes of a volume group) onto new 4K
    volumes, then swap: the originals are detached and kept as rollback copies."""
    r = st["receiver"]
    attached = len(r["volumes"])
    if attached + len(exports) > planmod.MAX_VOLUMES:
        die(f"rebuilding {', '.join(exports)} needs {len(exports)} spare volume(s) next to the {attached} "
            f"attached, more than the {planmod.MAX_VOLUMES} an instance takes. Source VM is off: "
            f"`exo-v2v rollback {st.name}` restarts it")
    spares = {}
    for export in exports:
        vol = next(v for v in r["volumes"] if v["export"] == export)
        size = volume_size_gb(ex.volume(vol["name"]))
        spare = f"{vol['name']}-4k"
        log(f"{export}: rebuilding 512-byte-sector filesystems onto new 4K volume {spare}")
        ex.volume_create(spare, size, {LABEL: st.name})
        ex.volume_attach(spare, r["name"])
        spares[export] = (vol, spare, wait_device(t, ex.volume(spare)["id"][:20]))
    t.post("/relayout", {"spares": {e: dev for e, (_v, _s, dev) in spares.items()}})
    while any(t.get("/relayout")[e]["state"] == "running" for e in exports):
        time.sleep(10)
    res = t.get("/relayout")
    bad = {e: res[e].get("error") or res[e].get("report", {}).get("problems") for e in exports
           if res[e]["state"] != "done"}
    if bad:
        die(f"relayout failed: {bad}. Source VM is off: `exo-v2v rollback {st.name}` restarts it")
    for export, (vol, spare, _dev) in spares.items():
        ex.volume_detach(vol["name"])
        ex.volume_label(vol["name"], {LABEL: st.name, "exo-v2v-rollback": export})
        vol.update(rollback=vol["name"], name=spare, serial=ex.volume(spare)["id"][:20])
        st.save()
        steps = res[export]["report"]["steps"]
        log(f"{export}: relayout done ({[x.get('method') for x in steps if x.get('method')]}); "
            f"original kept as rollback")


def cmd_cutover(a):
    st = State.load(a.name)
    if st["phase"] not in ("syncing", "receiver-ready", "cutover"):
        die(f"phase is {st['phase']}")
    p, r = st["plan"], st["receiver"]
    ex = exo_for(st, a)
    V, vc, vm, proxy = sender_context(a, st)
    reconcile_disks(st, a, ex, V, vm)    # before the downtime starts
    t_down = time.time()
    try:
        with tunnel_for(st) as t:
            st.set_phase("cutover")
            cyc = run_cycle(st, a, V, vc, vm, proxy, t, final=True)
            log(f"final cycle: {cyc.get('power')}, {cyc['seconds']}s")
            if a.verify:
                res = V.verify(vc, vm, mapping_of(st), t.api, transport=a.transport, proxy=proxy)
                if any(x["mismatched"] for x in res.values()):
                    die(f"VERIFY FAILED: {res}. Source stays off; run `exo-v2v rollback {a.name}` to restart it")
            if p["os_family"] == "windows":
                win_support(t)      # no-op when staged at create time
            log("converting (virt-v2v-in-place on the receiver, then 4Kn + network fix-ups)")
            t.post("/convert")
            while (c := t.get("/convert"))["state"] == "running":
                time.sleep(15)
            st.data["convert"] = {k: c.get(k) for k in ("state", "rc", "error", "problems", "adapt", "netfix")}
            st.save()
            if c["state"] != "done":
                die(f"conversion failed: {c.get('error')}\n" + "\n".join(c.get("log_tail", [])[-15:]))
            if c.get("fold"):
                log(f"folded into the root disk: {c['fold']['added']}")
            groups = c.get("relayout_groups") or []
            grouped = {e for g in groups for e in g}
            for export, probs in (c.get("unfixable") or {}).items():
                st.event(f"{export}: left as is: {probs}")
                log(f"WARNING {export}: {'; '.join(probs)} — not rebuilt; check it in the guest")
            for export, v in (c.get("adapt") or {}).items():
                rest = [x for x in v.get("problems", []) if export not in grouped and
                        x not in (c.get("unfixable") or {}).get(export, [])]
                if rest:
                    st.event(f"{export}: {rest}")
                    log(f"WARNING {export}: {'; '.join(rest)}")
            for g in sorted(groups, key=len):
                relayout_group(st, ex, t, g)
            t.post("/flush")
    finally:
        vc.close()
    st.set_phase("converted")
    log(f"restarting {r['name']} from its own disks")
    ex.instance_stop(r["name"])
    if p["target_type"] != p["convert_type"]:
        log(f"scaling {p['convert_type']} -> {p['target_type']}")
        ex.instance_scale(r["name"], p["target_type"])
    ex.instance_start(r["name"])
    for sg in p["security_groups"]:
        ex.instance_sg_add(r["name"], sg)
    port = 3389 if p["os_family"] == "windows" else 22
    if port == 3389:
        try:
            ex.sg_rule_add(r["sg"], r["sender_cidr"], 3389, desc="exo-v2v sender (RDP check)")
        except ExoError:
            pass
    up = wait_port(r["ip"], port, a.boot_timeout)
    st.data["downtime_s"] = round((up or time.time()) - t_down)
    st.set_phase("done")
    print(f"cutover {'complete' if up else 'finished, but port %d not reachable yet' % port}: "
          f"{r['name']} {r['ip']} — downtime {st['downtime_s']}s (source shutdown -> port {port} open)")
    if port == 3389:
        print("note: Windows keeps installing drivers for a few minutes (it may reboot); data disks "
              "come online when that finishes (C:\\Program Files\\Guestfs\\Firstboot\\log.txt)")
    print(f"next: test the workload, then `exo-v2v cleanup {a.name}` (or `rollback`)")


def wait_port(host, port, timeout):
    import socket
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection((host, port), timeout=5):
                return time.time()
        except OSError:
            time.sleep(5)
    return None


def cmd_status(a):
    names = [a.name] if a.name else State.all()
    for n in names:
        st = State.load(n)
        p, r = st["plan"], st["receiver"]
        last = st["cycles"][-1] if st["cycles"] else None
        print(f"{n}: {p['vm']} -> {r.get('name', '-')} ({r.get('ip', '-')}) in {p['zone']}  phase={st['phase']}")
        if last:
            age = int(time.time() - last["t"])
            print(f"  cycles={len(st['cycles'])} last={age}s ago "
                  f"({', '.join(f'{k}:{v['mode']} {human(v['covered'])}' for k, v in last['disks'].items())})")
        if "downtime_s" in st.data:
            print(f"  downtime {st['downtime_s']}s")
        if a.verbose:
            print_plan(p)


def cmd_rollback(a):
    st = State.load(a.name)
    V, vc = vsphere_connect(a)
    try:
        vm = vc.vm(st["plan"]["vm"])
        if str(vm.runtime.powerState) != "poweredOn":
            V.WaitForTask(vm.PowerOnVM_Task())
            log(f"source VM {vm.name} powered on")
    finally:
        vc.close()
    ex = exo_for(st, a)
    if st["receiver"].get("name") and ex.instance_exists(st["receiver"]["name"]):
        ex.instance_stop(st["receiver"]["name"])
        log(f"receiver {st['receiver']['name']} stopped (delete it with cleanup --delete-instance)")
    st.set_phase("rolled-back")


def cmd_cleanup(a):
    st = State.load(a.name)
    ex = exo_for(st, a)
    r = st["receiver"]
    if a.delete_instance and r.get("name") and ex.instance_exists(r["name"]):
        if st["phase"] == "done" and not a.force:
            die("migration is done: refusing to delete the migrated instance without --force")
        ex.instance_delete(r["name"])
        for v in r.get("volumes", []):
            for n in (v["name"], v.get("rollback")):
                if n:
                    try:
                        ex.volume_delete(n)
                    except ExoError:
                        pass
        log(f"deleted {r['name']} and its volumes")
    elif r.get("name"):
        if a.delete_rollback_volumes:
            for v in r.get("volumes", []):
                if v.get("rollback"):
                    ex.volume_delete(v["rollback"])
                    log(f"deleted rollback volume {v['rollback']}")
                    v.pop("rollback")
        try:
            ex.instance_sg_remove(r["name"], r["sg"])
        except ExoError:
            pass
    try:
        ex.sg_delete(r["sg"])
        log(f"deleted security group {r['sg']}")
    except (ExoError, KeyError):
        pass
    st.set_phase("cleaned")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="exo-v2v", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"exo-v2v {__version__}")
    ap.add_argument("--account", help="exo CLI account")
    ap.add_argument("--vsphere-host"); ap.add_argument("--vsphere-user")
    ap.add_argument("--vsphere-verify-tls", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("plan"); s.add_argument("name"); s.add_argument("--vm", required=True)
    s.add_argument("--zone", required=True); s.add_argument("--instance-type")
    s.add_argument("--system-disk", help="disk holding the OS (default: from the boot order or "
                   "VMware Tools), e.g. scsi0:0")
    s.add_argument("--fold", action="append", metavar="DISK",
                   help="carry DISK inside the root disk instead of a Block Storage volume (repeatable; "
                        "automatic for disks sharing a filesystem with the system disk)")
    s.add_argument("--no-fold", action="store_true", help="never fold disks automatically")
    s.add_argument("--security-group", action="append", help="attach to the instance at cutover")
    s.add_argument("--receiver-template", help="exo-v2v receiver template to create the instance from "
                   "(default: a stock Linux template, rebuilt into RAM at `create`)")
    s.add_argument("--receiver-template-visibility", choices=["public", "private"], default="public")
    s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("create"); s.add_argument("name"); s.add_argument("--instance-name")
    s.add_argument("--sender-cidr", help="source address of the SSH tunnel (default: detected public IP/32)")
    s.add_argument("--timeout", type=int, default=1800); s.set_defaults(fn=cmd_create)

    for nm, fn in (("sync", cmd_sync), ("cutover", cmd_cutover)):
        s = sub.add_parser(nm); s.add_argument("name")
        s.add_argument("--transport", choices=["hotadd", "http"], default="hotadd")
        s.add_argument("--proxy-vm"); s.add_argument("--workers", type=int, default=4)
        s.add_argument("--shutdown-timeout", type=int, default=300)
        s.add_argument("--timeout", type=int, default=1800,
                       help="receiver readiness timeout when it must be reset (a grown system disk)")
        s.set_defaults(fn=fn)
        if nm == "sync":
            s.add_argument("--full", action="store_true")
            s.add_argument("--every", type=int, help="repeat every N seconds (Ctrl-C stops after the cycle)")
            s.add_argument("--cycles", type=int)
        else:
            s.add_argument("--verify", action="store_true", help="hash-compare source and receiver (adds downtime)")
            s.add_argument("--boot-timeout", type=int, default=900)

    s = sub.add_parser("status"); s.add_argument("name", nargs="?"); s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("rollback"); s.add_argument("name"); s.set_defaults(fn=cmd_rollback)
    s = sub.add_parser("cleanup"); s.add_argument("name")
    s.add_argument("--delete-rollback-volumes", action="store_true")
    s.add_argument("--delete-instance", action="store_true", help="delete the receiver/instance and its volumes")
    s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_cleanup)
    s = sub.add_parser("list"); s.set_defaults(fn=lambda a: [print(n) for n in State.all()])

    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except (ExoError, FileNotFoundError, FileExistsError) as e:
        die(str(e))


if __name__ == "__main__":
    main()
