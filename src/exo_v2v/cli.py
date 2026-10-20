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
from .util import die, human, log

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
        print(f"  {d['disk']:<9} {d['label']:<14} {human(d['capacity']):>10} -> {d['role']:<6} "
              f"{d['size_gb']} GB (export {d['export']})")
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
                      a.receiver_template, a.receiver_template_visibility, boot_mode)
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
    for i, gb in enumerate(p["volumes_gb"], 1):
        vname = f"v2v-{a.name}-d{i}"   # `v2v-` prefix: the IAM template scopes volumes by name
        ex.volume_create(vname, gb, labels)
        ex.volume_attach(vname, name)
        st.data["receiver"]["volumes"].append({"name": vname, "export": p["disks"][i]["export"]})
        st.save()
        log(f"volume {vname} {gb} GB attached")
    install_agent(st, 1 + len(p["volumes_gb"]), timeout=a.timeout, template_mode=template_mode)
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
    r = subprocess.run(t.ssh_base() + [f"{t.user}@{t.host}", "sudo sh -c " + repr(cmd)],
                       input=userdata.payload_tgz(), capture_output=True, timeout=300)
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
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True) or log("stopping after this cycle"))
    try:
        with tunnel_for(st) as t:
            st.set_phase("syncing")
            n = 0
            while True:
                cyc = run_cycle(st, a, V, vc, vm, proxy, t, full=a.full and n == 0)
                n += 1
                log(f"cycle {len(st['cycles'])}: {sum(d['covered'] for d in cyc['disks'].values()) / 2**20:.0f} MiB "
                    f"in {cyc['seconds']}s")
                if n == 1:
                    preflight_report(st, t)
                if not a.every or stop["now"] or (a.cycles and n >= a.cycles):
                    break
                for _ in range(a.every):
                    if stop["now"]:
                        break
                    time.sleep(1)
                if stop["now"]:
                    break
                t.ensure()
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
            if " ntfs " in f" {prob} ":
                how = "boot sector rewritten for 4K sectors at cutover (in place, no copy)"
            elif any(t in prob for t in (" xfs ", " ext ")):
                how = "rebuilt onto a 4K volume at cutover (adds time proportional to its data)"
            elif "not 4 KiB aligned" in prob:
                how = "NOT handled automatically (unaligned partition): see docs/OPERATIONS.md"
            else:
                how = "NOT handled automatically: see docs/OPERATIONS.md"
            log(f"preflight {export}: {prob} -> {how}")


def relayout_volume(st, ex, t, export):
    r = st["receiver"]
    vol = next(v for v in r["volumes"] if v["export"] == export)
    serial = next(d["serial"] for d in t.get("/status")["disks"] if d["export"] == export)
    size = volume_size_gb(ex.volume(vol["name"]))
    spare = f"{vol['name']}-4k"
    log(f"{export}: rebuilding 512-byte-sector filesystems onto new 4K volume {spare}")
    ex.volume_create(spare, size, {LABEL: st.name})
    ex.volume_attach(spare, r["name"])
    sid = ex.volume(spare)["id"][:20]
    dev = None
    for _ in range(60):
        dev = next((d["dev"] for d in t.get("/status")["spares"] if d["serial"] == sid), None)
        if dev:
            break
        time.sleep(5)
    if not dev:
        die(f"spare volume {spare} never appeared on the receiver")
    t.post(f"/relayout?export={export}&spare={dev}")
    while (s := t.get("/relayout")[export])["state"] == "running":
        time.sleep(10)
    if s["state"] != "done":
        die(f"relayout of {export} failed: {s.get('error') or s.get('report', {}).get('problems')}")
    ex.volume_detach(vol["name"])
    ex.volume_label(vol["name"], {LABEL: st.name, "exo-v2v-rollback": export})
    vol.update(rollback=vol["name"], name=spare)
    st.save()
    log(f"{export}: relayout done ({[x.get('method') for x in s['report']['steps']]}); original kept as rollback")


def cmd_cutover(a):
    st = State.load(a.name)
    if st["phase"] not in ("syncing", "receiver-ready", "cutover"):
        die(f"phase is {st['phase']}")
    p, r = st["plan"], st["receiver"]
    ex = exo_for(st, a)
    V, vc, vm, proxy = sender_context(a, st)
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
            for export, v in (c.get("adapt") or {}).items():
                if v.get("compatible", True):
                    continue
                fss = v.get("filesystems", [])
                if all(f["ok_on_4kn"] or f["type"] in ("xfs", "ext") for f in fss):
                    relayout_volume(st, ex, t, export)
                else:
                    # e.g. NTFS (512 bytes/sector): the partition table was rescaled;
                    # whether the guest mounts it on 4Kn is checked after boot
                    st.event(f"{export}: left as is: {v['problems']}")
                    log(f"WARNING {export}: {'; '.join(v['problems'])} — not rebuilt; check it in the guest")
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
    s.add_argument("--system-disk", help="disk holding the OS (default: first disk), e.g. scsi0:0")
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
