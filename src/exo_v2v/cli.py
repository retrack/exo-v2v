"""exo-v2v — warm (online) migration of VMware vSphere VMs to Exoscale.

Workflow (each step is resumable; state in ~/.exo-v2v/<name>/):

  exo-v2v plan     NAME --vm VMNAME --zone ZONE [--instance-type T] [--security-group SG ...]
  exo-v2v create   NAME                 # receiver instance + volumes, RAM-boots the agent
  exo-v2v sync     NAME [--full] [--every SECONDS] [--cycles N]
  exo-v2v cutover  NAME [--verify]      # final cycle (VM powered off), convert, start on Exoscale
  exo-v2v status   [NAME]
  exo-v2v rollback NAME                 # power the source VM back on; receiver stopped
  exo-v2v recover  NAME                 # a receiver that restarted: reset its root disk, keep the volumes
  exo-v2v cleanup  NAME [--delete-rollback-volumes] [--delete-instance] [--rename NEW]
  exo-v2v group    discover|plan|create|sync|cutover|status|rollback|cleanup GROUP ...
  exo-v2v web      [--listen 127.0.0.1:8780]   # read-only status page

vSphere connection: VSPHERE_HOST, VSPHERE_USER, VSPHERE_PASSWORD (env) or
--vsphere-host/--vsphere-user; vCenter or standalone ESXi. Run sync/cutover as
root inside the replication appliance VM (hot-add transport).
Exoscale: the `exo` CLI's configured account (exo config) or --account.
"""
import argparse
import contextlib
import json
import os
import signal
import subprocess
import tempfile
import time
import urllib.request

from . import __version__
from . import alerts, public, service, wan
from . import plan as planmod
from . import userdata
from .exo import Exo, ExoError, volume_size_gb
from .state import BadName, State, check_name
from .tunnel import Tunnel
from . import util
from .util import die, gib_ceil, human, log

LABEL = "exo-v2v"
# The receiver's security group allows no egress until the cutover: Exoscale turns
# a group's egress into an allowlist at its first egress rule, and this one
# matches nothing (TEST-NET-1). Rules whose description starts with "exo-v2v:"
# are exo-v2v's own and go at the cutover.
EGRESS_CLOSED = "exo-v2v: no egress before the cutover"
EGRESS_SINK = "192.0.2.1/32"


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
    if getattr(a, "sender_cidr", None):
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
    return Tunnel(r["ip"], st["plan"]["receiver_user"], st.key_path, os.path.join(st.dir, "known_hosts"),
                  jump=r.get("jump"))


def mapping_of(st):
    return {d["disk"]: d["export"] for d in st["plan"]["disks"]}


def print_plan(p):
    inv = p.get("inventory") or {}
    where = f" on {inv['server']['product']}" if inv.get("server") else ""
    path = f" [{inv['path']}]" if inv.get("path") and inv["path"].rsplit("/", 1)[-1] != inv["path"] else ""
    print(f"VM {p['vm']}{path}: {p['os_family']} ({p['guest_id']}), firmware {p['firmware']}{where}")
    print(f"  receiver: {p['template']} as {p['convert_type']} -> final {p['target_type']} in {p['zone']}")
    for d in p["disks"]:
        where = (f"inside the root disk at {human(d['offset'])}" if d["role"] == "fold"
                 else f"{d['size_gb']} GB")
        print(f"  {d['disk']:<9} {d['label']:<14} {human(d['capacity']):>10} -> {d['role']:<6} "
              f"{where} (export {d['export']})")
    pub = p.get("public") or {}
    if pub.get("elastic_ip"):
        print(f"  public: Elastic IP {'(new, reserved at create)' if pub['elastic_ip'] == 'new' else pub['elastic_ip']}"
              ", attached and configured in the guest at cutover")
    if pub.get("reverse_dns"):
        print(f"  reverse DNS: {pub['reverse_dns']}")
    for n in pub.get("dns_records") or []:
        print(f"  DNS {n}: A record -> {'the Elastic IP' if pub.get('elastic_ip') else 'the instance'} at cutover "
              f"(TTL {pub.get('dns_ttl', 60)}s from create on)")
    for n in p.get("networks") or []:
        if n.get("from"):
            print(f"  renumbered: {n['from']} -> {n['ip']} on {n['network']}")
    for k in ("errors", "warnings", "notes"):
        for m in p[k]:
            print(f"  {k[:-1].upper()}: {m}")


# ---------------- commands ----------------

def vsphere_warnings(V, vc, vm):
    """What the vSphere side will refuse later: missing privileges of the
    account, an appliance host that cannot see the VM's datastores, DRS."""
    out = []
    proxy = V.self_vm(vc) if os.geteuid() == 0 else None      # plan run on the appliance, as root
    missing = V.missing_privileges(vc, vm, proxy)
    if missing:
        out.append("vSphere account lacks " + "; ".join(missing) + " (see docs/prerequisites.md)")
    if proxy is not None:
        errs, warns = V.appliance_checks(vc, vm, proxy)
        out += errs + warns
    return out


def cmd_plan(a):
    check_name(a.name)
    V, vc = vsphere_connect(a)
    try:
        vm = vc.vm(a.vm)
        inv = V.inventory(vc, vm)
        vwarn = vsphere_warnings(V, vc, vm)
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
    p["warnings"] += vwarn
    p["public"] = public_of(a.elastic_ip, a.reverse_dns, a.dns_record, a.dns_ttl, p["errors"])
    if p["public"]["dns_records"]:
        p["errors"] += public.check(Exo(a.zone, a.account), p["public"]["dns_records"])
    print_plan(p)
    if p["errors"] and not a.force:
        die("plan has errors (fix them, or --force to record the plan anyway)")
    st = State.new(a.name, p)
    log(f"plan saved: {st.path}")


def public_of(elastic_ip=None, reverse_dns=None, dns_records=(), dns_ttl=60, errors=None):
    """The plan's public side: {elastic_ip: "new"|address|None, reverse_dns, dns_records, dns_ttl}."""
    import ipaddress
    if elastic_ip and elastic_ip != "new":
        try:
            ipaddress.IPv4Address(elastic_ip)
        except ValueError:
            errors.append(f"--elastic-ip {elastic_ip!r}: `new` or an Elastic IP address of the zone")
    return {"elastic_ip": elastic_ip, "reverse_dns": reverse_dns, "dns_records": list(dns_records or []),
            "dns_ttl": int(dns_ttl or 60)}


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
                                  template_visibility=p.get("template_visibility", "public"),
                                  anti_affinity_groups=p.get("anti_affinity_groups", []))
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
    attach_networks(st, ex)
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
    setup_public(st, ex)
    close_egress(st, ex)
    st.set_phase("receiver-ready")
    print(f"receiver {name} ready at {st['receiver']['ip']}"
          + (f", Elastic IP {st['receiver']['eip']['ip']} (attached at cutover)" if st["receiver"].get("eip") else ""))


def setup_public(st, ex):
    """create: the Elastic IP reserved, reverse DNS, DNS TTLs lowered (single migrations;
    a group lowers its DNS records itself)."""
    pub = st["plan"].get("public") or {}
    r = st["receiver"]
    if pub.get("elastic_ip"):
        public.reserve_eip(st, ex)
        if not any(n["network"] == "public" for n in r.get("nics", [])):
            r["nics"] = ex.instance_nics(r["name"])          # the public NIC's MAC, for the guest
            st.save()
    public.set_reverse_dns(st, ex)
    if pub.get("dns_records") and not st.data.get("dns"):
        st.data["dns"] = public.prepare(ex, [(n, st.name) for n in pub["dns_records"]], pub.get("dns_ttl", 60))
        st.save()


def attach_networks(st, ex):
    """Private networks of the plan, attached in order (the guest's NICs after
    the public one), each address as a static lease; their MACs, known from now
    on, drive the guest's network fix-up at cutover."""
    p, r = st["plan"], st["receiver"]
    nets = [n for n in p.get("networks", []) if n["network"] != "public"]
    if not p.get("networks"):
        return                         # a single migration: no network map
    have = {n["network"] for n in r.get("nics", [])}
    for n in nets:
        if n["network"] not in have:
            ex.privnet_attach(r["name"], n["network"], n.get("ip"))
            log(f"private network {n['network']} attached" + (f" ({n['ip']}, static lease)" if n.get("ip") else ""))
    ids = {ex.privnet(n["network"])["id"]: n["network"] for n in nets}
    r["nics"] = [{**x, "network": ids.get(x["network"], x["network"])} for x in ex.instance_nics(r["name"])]
    st.save()


def netmap_of(st):
    """Target NICs for the guest's network fix-up: [{"mac", "mode", "ip", "prefix", "nogw"}]."""
    p, r = st["plan"], st["receiver"]
    macs = {n["network"]: n["mac"] for n in r.get("nics", [])}
    out = []
    for n in p.get("networks", []):
        if not macs.get(n["network"]):
            die(f"the receiver has no NIC on {n['network']} (`exo-v2v create` attaches it)")
        e = {"mac": macs[n["network"]], "mode": n["mode"]}
        if n.get("ip"):
            e.update(ip=n["ip"], prefix=n["prefix"], nogw=bool(n.get("nogw")))
        if n.get("from"):
            e["from"] = n["from"]
        out.append(e)
    if r.get("eip"):
        if not macs.get("public"):
            die("the public NIC's MAC is unknown (`exo-v2v create` records it)")
        if not out:
            out.append({"mac": macs["public"], "mode": "dhcp"})
        out.append({"mac": macs["public"], "mode": "eip", "ip": r["eip"]["ip"], "prefix": 32})
    return out


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
    payload = agent_payload(st)
    r = subprocess.run(t.ssh_base() + [f"{t.user}@{t.host}", "sudo sh -c " + repr(cmd)],
                       input=payload, capture_output=True, timeout=300)
    if r.returncode:
        die(f"receiver: agent install failed: {r.stderr.decode(errors='replace')[-400:]}")
    chk = t.ssh("test -s /opt/v2v-agent/agent.py && test -s /opt/v2v-agent/bootstrap-kexec.sh", timeout=60)
    if chk.returncode:
        die("receiver: agent payload missing after install")
    log("agent payload installed; " + ("agent started" if template_mode else "bootstrap started"))


def agent_payload(st):
    return userdata.payload_tgz(st["plan"].get("folds", []),
                                {v["export"]: v["serial"] for v in st["receiver"]["volumes"] if v.get("serial")},
                                userdata.boot_mask(st["plan"]))


def upgrade_agent(st, t, old=None):
    """A receiver whose agent is another version than the appliance's gets the
    current agent in place, between two cycles: same boot, same disks, so its
    change IDs are kept. An agent older than the boot mask (exo-v2v < 0.4.1)
    then masks the boot sectors it finds on the disk."""
    exports = sorted(set(mapping_of(st).values()))
    saved = {e: t.get(f"/meta?export={e}") for e in exports}
    log(f"receiver agent {old or 'older than 0.4.1 (no boot mask)'} -> {__version__}: upgrading it in place "
        "(change IDs kept)")
    cmd = userdata.agent_start_command(1 + len(st["receiver"]["volumes"]))
    r = subprocess.run(t.ssh_base() + [f"{t.user}@{t.host}", "sudo sh -c " + repr(cmd)],
                       input=agent_payload(st), capture_output=True, timeout=600)
    if r.returncode:
        die(f"receiver: agent upgrade failed: {r.stderr.decode(errors='replace')[-400:]}")
    status = {}
    for _ in range(90):
        try:
            status = t.get("/status", timeout=5)
            if status.get("version") == __version__:
                break
        except OSError:
            pass
        time.sleep(2)
    else:
        die("receiver: the upgraded agent did not come back")
    for e, m in saved.items():
        if m.get("change_id"):
            t.post(f"/meta?export={e}", m)
    st.data["receiver"]["agent_started"] = time.time() - status.get("uptime_s", 0)
    st.event(f"receiver agent upgraded in place to {__version__}")
    st.save()
    return status


def close_egress(st, ex):
    """No egress from the receiver until the cutover (the appliance's SSH is
    inbound, its replies are stateful): even a copy of the source booted by
    accident reaches nothing. Template receivers only: a stock-template receiver
    needs package mirrors again if its root disk is ever reset."""
    r = st["receiver"]
    if st["plan"].get("receiver_mode") != "template" or r.get("egress_closed") or not r.get("sg"):
        return
    if not any(x.get("description") == EGRESS_CLOSED for x in ex.sg_egress_rules(r["sg"])):
        ex.sg_rule_add(r["sg"], EGRESS_SINK, 9, proto="udp", desc=EGRESS_CLOSED, flow="egress")
    r["egress_closed"] = True
    st.save()
    log(f"egress of {r['name']} closed until the cutover (security group {r['sg']}; applies within ~2 min)")


def open_egress(st, ex):
    """Before the cutover stops the source: the migrated VM gets its normal egress
    (the deletion takes a minute or two to apply, less than the cutover itself)."""
    r = st["receiver"]
    if not r.get("sg"):
        return
    try:
        rules = [x for x in ex.sg_egress_rules(r["sg"]) if (x.get("description") or "").startswith("exo-v2v:")]
    except ExoError:
        rules = []                     # no security group any more
    for x in rules:
        try:
            ex.sg_rule_delete(r["sg"], x["id"])
        except ExoError as e:
            die(f"cannot reopen the egress of {r['name']} ({e}). Update the appliance's IAM role with "
                "docs/iam/exo-v2v-appliance-policy.json (delete-rule-from-security-group); nothing was stopped")
    if rules:
        log(f"egress of {r['name']} reopened for the migrated VM")
    if r.get("egress_closed"):
        r["egress_closed"] = False
        st.save()


def host_key(ip, timeout=8):
    """The ed25519 host key `ip` presents, or None when nothing answers."""
    try:
        out = subprocess.run(["ssh-keyscan", "-T", str(timeout), "-t", "ed25519", ip],
                             capture_output=True, text=True, timeout=timeout + 10).stdout
    except subprocess.TimeoutExpired:
        return None
    keys = [ln.split()[2] for ln in out.splitlines() if len(ln.split()) >= 3 and not ln.startswith("#")]
    return keys[0] if keys else None


def receiver_down(st, ex, err):
    """The receiver's agent is unreachable: tell a restarted receiver from a
    network problem, stop a receiver that booted its replica, and die."""
    r = st["receiver"]
    try:
        state = ex.instance(r["name"]).get("state")
    except ExoError as e:
        die(f"receiver {r['name']} unreachable ({err}); Exoscale: {e}")
    rec = f"`exo-v2v recover {st.name}`"
    known_down = bool(r.get("down"))                # already reported: `recover` clears it

    def down(msg):                                  # the receiver needs a person: alert (once), then stop
        if not known_down:
            alerts.send("receiver-down", f"{st.name}: {msg}", migration=st.name, group=st.data.get("group"))
        die(msg)
    if state != "running":
        r["down"] = {"t": time.time(), "state": state, "trust_volumes": True}
        st.save()
        down(f"receiver {r['name']} is {state}. Its agent ran from RAM: {rec} resets its root disk "
            "(copied again at the next sync) and keeps the volumes' copies")
    key = host_key(r["ip"])
    kh = os.path.join(st.dir, "known_hosts")
    known = set()
    if os.path.exists(kh):
        known = {ln.split()[2] for ln in open(kh) if len(ln.split()) >= 3 and "ed25519" in ln}
    if key and known and key not in known:
        r["down"] = {"t": time.time(), "state": "booted its disk", "trust_volumes": False}
        st.event("receiver restarted and booted the copy on its disk: stopped")
        st.save()
        try:
            ex.instance_stop(r["name"])
            stopped = "exo-v2v stopped it: a copy of the source must not run next to the source."
        except ExoError as e:
            stopped = f"STOP IT NOW (exo compute instance stop {r['name']}): {e}"
        down(f"receiver {r['name']} restarted and booted the copy of the source VM on its disk "
            f"(receivers created before exo-v2v 0.4.1 have no boot mask). {stopped} {rec} resets its "
            "root disk; the volumes are copied again in full (the copy may have written to them)")
    if not key:
        r["down"] = {"t": time.time(), "state": "no SSH", "trust_volumes": True}
        st.save()
        down(f"receiver {r['name']} is running but does not answer on SSH: it most likely restarted "
            f"(with the boot mask, nothing boots). {rec} resets its root disk (copied again at the next "
            f"sync) and keeps the volumes' copies. ({err})")
    down(f"receiver {r['name']} answers on SSH but its agent is unreachable: {err}")


@contextlib.contextmanager
def receiver(st, ex):
    """tunnel_for(st), started, with a diagnosis when the receiver is down."""
    t = tunnel_for(st)
    try:
        t.start()
    except ConnectionError as e:
        receiver_down(st, ex, e)
    try:
        yield t
    finally:
        t.stop()


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


def check_receiver_continuity(st, t, status):
    """An agent that restarted lost its RAM state (/meta): in the same boot (same
    disks, nothing wrote to them) the change IDs saved after the last cycle are
    restored; otherwise the next cycle is a full copy - vsync does that when
    /meta is empty."""
    r = st["receiver"]
    started = time.time() - status.get("uptime_s", 0)
    prev = r.get("agent_started")
    if prev and started - prev > 120:
        if status.get("boot_id") and status["boot_id"] == r.get("boot_id") and r.get("meta"):
            back = [e for e, m in r["meta"].items()
                    if m.get("change_id") and not t.get(f"/meta?export={e}").get("change_id")]
            for e in back:
                t.post(f"/meta?export={e}", r["meta"][e])
            log("receiver agent restarted in the same boot: change IDs restored" + (f" ({', '.join(back)})" if back else ""))
        else:
            log("receiver agent restarted since the last cycle: next cycle is a full copy")
    r["agent_started"] = started


def remember_meta(st, t, status):
    """The change IDs on the appliance as well (the agent keeps them in RAM), with
    the boot they belong to and whether the boot mask was on: `recover` and an
    agent restart use them."""
    r = st["receiver"]
    r["meta"] = {e: t.get(f"/meta?export={e}") for e in sorted(set(mapping_of(st).values()))}
    r["boot_id"] = status.get("boot_id")
    r["mask_active"] = bool((status.get("mask") or {}).get("active"))
    st.save()


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
    if r.get("rebuild_reason") == "recover":
        log(f"resetting the root disk of {r['name']} to {p['template']} (the system disk is copied again in "
            "the next cycle; the volumes " + ("keep their copies)" if any(
                m.get("change_id") for m in (r.get("saved_meta") or {}).values()) else "are copied in full)"))
    else:
        log(f"the root disk must grow to {p['root_gb']} GB: resetting {r['name']} (its volumes are kept; "
            "the system disk is copied again in the next cycle)")
    if "saved_meta" not in r:
        with tunnel_for(st) as t:
            r["saved_meta"] = {v["export"]: t.get(f"/meta?export={v['export']}") for v in r["volumes"]}
        st.save()
    if ex.instance(r["name"]).get("state") != "stopped":
        ex.instance_stop(r["name"])                    # Exoscale resets stopped instances only
    for v in r["volumes"]:                             # (stopped first: a booted copy may use them)
        if ex.volume(v["name"]).get("state") == "attached":
            ex.volume_detach(v["name"])
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
        status = t.get("/status")
    why = r.pop("rebuild_reason", None)
    r["meta"] = r.pop("saved_meta")                # the volumes' change IDs, in this new boot
    r.update(boot_id=status.get("boot_id"), mask_active=bool((status.get("mask") or {}).get("active")),
             agent_started=time.time() - status.get("uptime_s", 0))
    r.pop("rebuild_pending", None); r.pop("down", None)
    st.event("receiver recovered (root disk reset)" if why == "recover"
             else f"receiver reset with a {p['root_gb']} GB root disk")
    st.save()
    log("receiver ready again")


def cmd_recover(a):
    """A receiver that restarted (stopped, unreachable, or booted its disk):
    reset its root disk to the receiver template and restart the agent. The
    volumes keep their copies (change IDs saved after the last cycle) when the
    boot mask was on and nothing booted; otherwise they are copied in full."""
    st = State.load(a.name)
    if st["phase"] not in ("receiver-ready", "syncing"):
        die(f"phase is {st['phase']}: recover is for a receiver during the replication")
    migration_lock(st, "recover")
    ex = exo_for(st, a)
    r = st["receiver"]
    if not r.get("rebuild_pending"):
        down = r.get("down") or {}
        trust = bool(r.get("mask_active")) and down.get("trust_volumes", True) and not a.full
        meta = r.get("meta") or {}
        r["saved_meta"] = {v["export"]: meta.get(v["export"], {}) for v in r["volumes"]} if trust else {}
        r["rebuild_reason"] = "recover"
        r["rebuild_pending"] = True                  # resumable: run recover again
        st.save()
    kept = any(m.get("change_id") for m in (r.get("saved_meta") or {}).values())
    rebuild_receiver(st, a, ex)
    vols = "" if not r["volumes"] else ", the volumes' changes only" if kept else ", the volumes in full"
    print(f"{st.name}: receiver recovered; the next `exo-v2v sync` copies the system disk in full{vols}")


def run_cycle(st, a, V, vc, vm, proxy, t, full=False, final=False):
    status = t.get("/status")
    if not status.get("ramroot"):
        die("receiver is not in its RAM root any more (it rebooted from disk?): recreate it (exo-v2v cleanup --delete-instance; create)")
    if status.get("version") != __version__ and not final:
        status = upgrade_agent(st, t, status.get("version"))
    check_receiver_continuity(st, t, status)
    cyc = V.sync(vc, vm, mapping_of(st), t.nbd, t.api, full=full, final=final,
                 shutdown_timeout=a.shutdown_timeout, workers=a.workers, transport=a.transport, proxy=proxy,
                 pacer=lambda final: wan.Pacer(final=final), snapshot_name=getattr(a, "snapshot", None))
    cyc["t"] = time.time()
    st.data["cycles"].append(cyc)
    st.save()
    remember_meta(st, t, status)
    return cyc


def migration_lock(st, what, fd=None):
    """One sync/cutover per migration at a time (the sweep and the receiver's
    changeId assume a single writer). The holder's pid goes into the file: the
    web page reads it (it never takes the lock). fd: the lock a group process
    holds for this child (--lock-fd; the same open file, so the lock is shared:
    the holder stays the group's, and nothing is unlocked here)."""
    import fcntl
    path = os.path.join(st.dir, ".lock")
    if fd is not None:
        if os.fstat(fd).st_ino != os.stat(path).st_ino:
            die(f"--lock-fd {fd} is not the lock of {st.name}")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(fd, False)
        st._lock = os.fdopen(fd, "a+")
        return
    lk = open(path, "a+")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        die(f"another exo-v2v sync/cutover of {st.name} is running")
    lk.seek(0); lk.truncate()
    lk.write(json.dumps({"pid": os.getpid(), "what": what, "since": time.time()}))
    lk.flush()
    st._lock = lk


def sender_context(a, st, what):
    migration_lock(st, what, getattr(a, "lock_fd", None))
    try:
        wan.config()                              # a broken wan: section stops here, not mid-copy
        if getattr(a, "bwlimit", None):
            log(f"WAN limit for this run (whole appliance): {wan.human_rate(wan.claim_override(a.bwlimit))}")
    except wan.WanError as e:
        die(str(e))
    V, vc = vsphere_connect(a)
    vm = vc.vm_of(st["plan"])
    proxy = None
    if a.transport == "hotadd":
        if os.geteuid() != 0:
            die("hot-add transport: run as root inside the replication appliance VM")
        proxy = vc.vm(a.proxy_vm) if a.proxy_vm else V.self_vm(vc)
        if proxy is None:
            die("cannot identify the appliance VM by SMBIOS UUID: pass --proxy-vm NAME")
        errs, warns = V.appliance_checks(vc, vm, proxy)     # the VM may have moved since the last cycle
        if errs:
            die("; ".join(errs))
        for w in warns:
            log(f"WARNING {w}")
    for msg in V.sweep(vc, vm, proxy, keep=getattr(a, "snapshot", None)):
        log(f"cleanup: {msg}")
    if not vm.config.changeTrackingEnabled or not all(V.disk_ctk(vm).values()):
        log("enabling CBT on the source VM (VM + every disk)")
        log(f"CBT: {V.enable_cbt(vc, vm)}")
    return V, vc, vm, proxy


def cmd_sync(a):
    """A cycle (or --every: cycles until Ctrl-C). Failed cycles are counted and,
    past alerts.failures in a row, alerted (alerts.py); a busy lock is not a failure."""
    try:
        sync_loop(a, State.load(a.name))
    except SystemExit as e:
        if e.code and not (util.LAST_ERROR["msg"] or "").startswith("another exo-v2v"):
            alerts.sync_failed(State.load(a.name))
        raise
    except Exception as e:
        alerts.sync_failed(State.load(a.name), repr(e))
        raise


def sync_loop(a, st):
    if st["phase"] not in ("receiver-ready", "syncing"):
        die(f"phase is {st['phase']}: sync needs a ready receiver")
    V, vc, vm, proxy = sender_context(a, st, "sync")
    if a.snapshot:
        a.every, a.cycles = None, 1          # one cycle from the group's consistent point
    ex = exo_for(st, a)
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True) or log("stopping after this cycle"))
    try:
        st.set_phase("syncing")
        close_egress(st, ex)
        n = 0
        while True:
            changed = reconcile_disks(st, a, ex, V, vm)
            with receiver(st, ex) as t:
                cyc = run_cycle(st, a, V, vc, vm, proxy, t, full=a.full and n == 0)
                n += 1
                log(f"cycle {len(st['cycles'])}: {sum(d['covered'] for d in cyc['disks'].values()) / 2**20:.0f} MiB "
                    f"in {cyc['seconds']}s")
                alerts.sync_ok(st)
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
        try:          # left by an interrupted cutover: reused, rebuilt again from scratch
            attached = ex.volume(spare).get("state") == "attached"
        except ExoError:
            ex.volume_create(spare, size, {LABEL: st.name})
            attached = False
        if not attached:
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


def group_guard(st, a, what):
    g = st.data.get("group")
    if g and not getattr(a, "in_group", False):
        die(f"{st.name} is a member of group {g}: use `exo-v2v group {what} {g}`")


def convert(st, ex, t, extra=None):
    """Conversion on the receiver (virt-v2v-in-place, 4Kn adaptation, network
    fix-up), then the volume rebuilds and a flush. Resumes after an interruption:
    a conversion that already finished is not run again."""
    steps = st.data["cutover_steps"]
    if st["plan"].get("networks") and not st["receiver"].get("nics"):
        attach_networks(st, ex)        # a public-only member created before 0.4.1 never recorded its NICs
    if "converted" not in steps:
        c = t.get("/convert")
        if c["state"] == "done" and steps.get("convert_started"):
            log("conversion already done on the receiver")
        else:
            if c["state"] == "failed":
                log(f"the previous conversion failed ({c.get('error')}): running it again")
            if c["state"] != "running":
                log("converting (virt-v2v-in-place on the receiver, then 4Kn + network fix-ups)")
                steps["convert_started"] = time.time()
                st.save()
                t.post("/convert", {"netmap": netmap_of(st), **(extra or {})})
            while (c := t.get("/convert"))["state"] == "running":
                time.sleep(15)
        st.data["convert"] = {k: c.get(k) for k in ("state", "rc", "error", "problems", "adapt", "netfix")}
        st.save()
        if c["state"] != "done":
            die(f"conversion failed: {c.get('error')}\n" + "\n".join(c.get("log_tail", [])[-15:]))
        if c.get("fold"):
            log(f"folded into the root disk: {c['fold']['added']}")
        st.data["relayout_groups"] = c.get("relayout_groups") or []
        grouped = {e for g in st.data["relayout_groups"] for e in g}
        for export, probs in (c.get("unfixable") or {}).items():
            st.event(f"{export}: left as is: {probs}")
            log(f"WARNING {export}: {'; '.join(probs)} — not rebuilt; check it in the guest")
        for export, v in (c.get("adapt") or {}).items():
            rest = [x for x in v.get("problems", []) if export not in grouped and
                    x not in (c.get("unfixable") or {}).get(export, [])]
            if rest:
                st.event(f"{export}: {rest}")
                log(f"WARNING {export}: {'; '.join(rest)}")
        steps["converted"] = time.time()
        st.save()
    vols = {v["export"]: v for v in st["receiver"]["volumes"]}
    for g in sorted(st.data.get("relayout_groups", []), key=len):
        if not all(vols[e].get("rollback") for e in g):      # not swapped yet
            relayout_group(st, ex, t, g)
    t.post("/flush")
    steps["flushed"] = time.time()
    st.save()


def cmd_cutover(a):
    try:
        cutover(a)
    except SystemExit as e:
        if e.code and not a.in_group and not (util.LAST_ERROR["msg"] or "").startswith("another exo-v2v"):
            alerts.send("cutover-failed", f"{a.name}: cutover stopped: {util.LAST_ERROR['msg']}", migration=a.name)
        raise


def cutover(a):
    st = State.load(a.name)
    group_guard(st, a, "cutover")
    from .rehearsal import active_for
    if active_for(st.name) and not (a.allow_rehearsal or a.in_group):
        die(f"rehearsal {active_for(st.name)} of {st.name} is up: `exo-v2v rehearsal destroy "
            f"{active_for(st.name)}` first, or --allow-rehearsal")
    ex = exo_for(st, a)
    if st["phase"] == "done" and dns_pending(st):
        return switch_dns(st, ex)
    if st["phase"] not in ("syncing", "receiver-ready", "cutover", "converted"):
        die(f"phase is {st['phase']}")
    steps = st.data.setdefault("cutover_steps", {})
    if st["phase"] != "converted":
        V, vc, vm, proxy = sender_context(a, st, "cutover")
        try:
            if "down_at" not in steps:
                reconcile_disks(st, a, ex, V, vm)    # before the downtime starts
            with receiver(st, ex) as t:
                if "source_off" not in steps:
                    open_egress(st, ex)
                    service.stop_for(migration=st.name)   # no timed cycles once the source stops
                if st["phase"] != "cutover":
                    st.set_phase("cutover")
                if "final_cycle" not in steps:
                    if "source_off" not in steps:
                        steps["down_at"] = steps.get("down_at") or time.time()
                        st.save()
                        steps["power"] = V.power_off(vm, a.shutdown_timeout)
                        steps["source_off"] = time.time()
                        st.event(f"source VM off ({steps['power']})")
                        log(f"source VM {vm.name}: {steps['power']}")
                    cyc = run_cycle(st, a, V, vc, vm, proxy, t, final=True)
                    steps["final_cycle"] = time.time()
                    st.save()
                    log(f"final cycle: {cyc['seconds']}s")
                if a.verify and "verified" not in steps:
                    res = V.verify(vc, vm, mapping_of(st), t.api, transport=a.transport, proxy=proxy)
                    if any(x["mismatched"] for x in res.values()):
                        die(f"VERIFY FAILED: {res}. Source stays off; run `exo-v2v rollback {a.name}` to restart it")
                    steps["verified"] = time.time()
                    st.save()
                if st["plan"]["os_family"] == "windows":
                    win_support(t)      # no-op when staged at create time
                convert(st, ex, t)
        finally:
            vc.close()
        st.set_phase("converted")
    if a.no_start:
        log(f"{st.name}: converted; the instance is not restarted yet (--no-start)")
        return
    stop_and_scale(st, ex)
    up = start_instance(st, ex, a.boot_timeout)
    r = st["receiver"]
    port = 3389 if st["plan"]["os_family"] == "windows" else 22
    print(f"cutover {'complete' if up else 'finished, but port %d not reachable yet' % port}: "
          f"{r['name']} {r['ip']} — downtime {st['downtime_s']}s (source shutdown -> port {port} open)")
    if dns_pending(st):
        if up:
            switch_dns(st, ex)
        else:
            log(f"DNS not switched (the instance does not answer): `exo-v2v cutover {st.name}` again "
                "switches it once it does, `rollback` otherwise")
    if not a.in_group:
        alerts.send("cutover-done", f"{st.name}: cut over to {r['name']} ({r['ip']}), downtime {st['downtime_s']}s"
                    + ("" if up else f", port {port} not reachable yet"), migration=st.name)
    if port == 3389:
        print("note: Windows keeps installing drivers for a few minutes (it may reboot); data disks "
              "come online when that finishes (C:\\Program Files\\Guestfs\\Firstboot\\log.txt)")
    print(f"next: test the workload, then `exo-v2v cleanup {a.name}` (or `rollback`)")


def dns_pending(st):
    return any(not r.get("switched") or r.get("restored") for r in st.data.get("dns") or [])


def switch_dns(st, ex):
    late = public.not_ready(st.data["dns"])
    if late:
        log(f"WARNING lowered TTLs not in effect everywhere yet: {', '.join(late)} (some resolvers "
            "keep the old address until their old TTL runs out)")
    public.switch(ex, st.data["dns"], lambda _m: public.address(st))
    st.data.setdefault("cutover_steps", {})["dns_switched"] = time.time()
    st.save()
    public.wait_served(ex, st.data["dns"])
    st.save()


def stop_and_scale(st, ex):
    """Converted receiver -> stopped instance of the target type (idempotent)."""
    p, r = st["plan"], st["receiver"]
    steps = st.data.setdefault("cutover_steps", {})
    if "stopped" not in steps:
        log(f"restarting {r['name']} from its own disks")
        if ex.instance(r["name"]).get("state") != "stopped":
            ex.instance_stop(r["name"])
        steps["stopped"] = time.time()
        st.save()
    if p["target_type"] != p["convert_type"] and "scaled" not in steps:
        log(f"scaling {p['convert_type']} -> {p['target_type']}")
        ex.instance_scale(r["name"], p["target_type"])
        steps["scaled"] = time.time()
        st.save()
    public.attach_eip(st, ex)


def start_instance(st, ex, boot_timeout, extra_ports=()):
    """Start the migrated instance, attach its security groups, wait for SSH/RDP
    (reached from here through the migration's own group). Returns the time
    the port answered, or None."""
    p, r = st["plan"], st["receiver"]
    steps = st.data["cutover_steps"]
    if ex.instance(r["name"]).get("state") != "running":
        ex.instance_start(r["name"])
    for sg in p["security_groups"]:
        if sg not in (ex.instance(r["name"]).get("security_groups") or []):
            ex.instance_sg_add(r["name"], sg)
    port = 3389 if p["os_family"] == "windows" else 22
    for x in ([3389] if port == 3389 else []) + list(extra_ports):
        if x not in r.setdefault("sg_ports", []):
            try:
                ex.sg_rule_add(r["sg"], r["sender_cidr"], x, desc=f"exo-v2v sender (check port {x})")
            except ExoError:
                pass
            r["sg_ports"].append(x)
    st.save()
    up = wait_port(r["ip"], port, boot_timeout)
    t_down = steps.get("down_at") or steps.get("source_off") or time.time()
    st.data["downtime_s"] = round((up or time.time()) - t_down)
    if up and r.get("eip"):
        # the guest configures the Elastic IP itself (Windows: once its drivers are in)
        eu = wait_port(r["eip"]["ip"], port, max(300, boot_timeout - round(up - t_down)))
        r["eip"]["answers"] = bool(eu)
        log(f"Elastic IP {r['eip']['ip']}: port {port} " + ("open" if eu else "NOT reachable (see the "
            "guest's /var/log/v2v-netfix.log, systemctl status v2v-eip, or C:\\ProgramData\\exo-v2v\\elastic-ip.log)"))
    steps["started"] = time.time()
    st.set_phase("done")
    return up


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
    names = [a.name] if a.name else [n for n in State.all() if not State.load(n).data.get("rehearsal_of")]
    for n in names:
        st = State.load(n)
        p, r = st["plan"], st["receiver"]
        last = st["cycles"][-1] if st["cycles"] else None
        grp = f"  group={st['group']}" if st.data.get("group") else ""
        if st.data.get("rehearsal_of"):
            grp += f"  rehearsal copy of {st['rehearsal_of']} ({st['rehearsal']})"
        eip = f", Elastic IP {r['eip']['ip']}" if r.get("eip") else ""
        print(f"{n}: {p['vm']} -> {r.get('name', '-')} ({r.get('ip', '-')}{eip}) in {p['zone']}  phase={st['phase']}{grp}")
        for d in st.data.get("dns") or []:
            print("  " + public.describe(d))
        if last:
            age = int(time.time() - last["t"])
            disks = ", ".join(f"{k}:{v['mode']} {human(v['covered'])}" for k, v in last["disks"].items())
            print(f"  cycles={len(st['cycles'])} last={age}s ago ({disks}{util.transfer(last)})")
        if "downtime_s" in st.data:
            print(f"  downtime {st['downtime_s']}s")
        if r.get("down"):
            print(f"  RECEIVER DOWN ({r['down']['state']}, {int(time.time() - r['down']['t'])}s ago): "
                  f"exo-v2v recover {n}")
        sy = st.data.get("sync") or {}
        if sy.get("failures"):
            print(f"  {sy['failures']} failed cycle(s) in a row; last error: {sy.get('last_error')}")
        if a.verbose:
            print_plan(p)


def rollback(st, a, V, vc, wait_tools=0):
    """Source VM back on. Before the conversion started the receiver is still a
    replica: it keeps running and `sync` continues (CBT goes on from the last
    cycle). After it, the receiver is stopped (its disks are converted)."""
    steps = st.data.get("cutover_steps", {})
    ex = exo_for(st, a)
    r = st["receiver"]
    converted = bool(steps.get("convert_started")) or st["phase"] in ("converted", "done")
    if converted and r.get("name") and ex.instance_exists(r["name"]):
        if ex.instance(r["name"]).get("state") != "stopped":
            ex.instance_stop(r["name"])
        log(f"{st.name}: instance {r['name']} stopped (delete it with cleanup --delete-instance)")
    if st.data.get("dns"):
        public.restore(ex, st.data["dns"])
        st.save()
    vm = vc.vm_of(st["plan"])
    waited = V.power_on(vm, wait_tools) if wait_tools else None
    if not wait_tools and str(vm.runtime.powerState) != "poweredOn":
        V.WaitForTask(vm.PowerOnVM_Task())
    log(f"{st.name}: source VM {vm.name} on" + (f" (VMware Tools up after {waited}s)" if waited else ""))
    if converted:
        st.set_phase("rolled-back")
    else:
        st.data["cutover_steps"] = {}
        st.set_phase("syncing" if st["cycles"] else "receiver-ready")
        log(f"{st.name}: the receiver was not converted: `sync` continues from the last cycle")


def cmd_rollback(a):
    st = State.load(a.name)
    if st.data.get("group"):
        log(f"{st.name} belongs to group {st['group']}: rolling back this member only")
    V, vc = vsphere_connect(a)
    try:
        rollback(st, a, V, vc)
    finally:
        vc.close()


def cmd_cleanup(a):
    cleanup(State.load(a.name), a)


def cleanup(st, a):
    service.stop_for(migration=st.name)
    ex = exo_for(st, a)
    r = st["receiver"]
    if st.data.get("dns"):
        public.finalize(ex, st.data["dns"])
        st.save()
    public.release_eip(st, ex, delete=getattr(a, "delete_elastic_ips", False))
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
    new = getattr(a, "rename", None) or st["plan"].get("final_name")
    if new and not a.delete_instance and st["phase"] == "done" and r.get("name") != new:
        ex.instance_rename(r["name"], new)     # hand-over: out of the appliance role's `v2v-` scope
        log(f"instance {r['name']} renamed {new}")
        r["name"] = new
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
    s.add_argument("--elastic-ip", metavar="new|ADDRESS",
                   help="an Elastic IP for the instance: reserved at create, attached and configured "
                        "in the guest at cutover")
    s.add_argument("--reverse-dns", metavar="NAME", help="PTR record of the Elastic IP (or the instance IP)")
    s.add_argument("--dns-record", action="append", metavar="FQDN",
                   help="A record on Exoscale DNS pointed at the instance at cutover (TTL lowered at create)")
    s.add_argument("--dns-ttl", type=int, default=60, help="TTL of --dns-record until cleanup (default 60)")
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
        s.add_argument("--in-group", action="store_true", help=argparse.SUPPRESS)
        s.add_argument("--bwlimit", metavar="RATE|off",
                       help="WAN limit for the whole appliance while this runs (e.g. 100Mbit), "
                            "instead of config.yaml's wan:")
        if nm == "sync":
            s.add_argument("--full", action="store_true")
            s.add_argument("--every", type=int, help="repeat every N seconds (Ctrl-C stops after the cycle)")
            s.add_argument("--cycles", type=int)
            s.add_argument("--snapshot", help=argparse.SUPPRESS)           # a group's consistent point
            s.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)  # the lock the group holds
        else:
            s.add_argument("--verify", action="store_true", help="hash-compare source and receiver (adds downtime)")
            s.add_argument("--boot-timeout", type=int, default=900)
            s.add_argument("--no-start", action="store_true",
                           help="stop after the conversion (the instance is started later)")
            s.add_argument("--allow-rehearsal", action="store_true",
                           help="cut over although a rehearsal of this migration is up")

    s = sub.add_parser("status"); s.add_argument("name", nargs="?"); s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("rollback"); s.add_argument("name"); s.set_defaults(fn=cmd_rollback)
    s = sub.add_parser("recover", help="a receiver that restarted: reset its root disk, keep the volumes' copies")
    s.add_argument("name"); s.add_argument("--timeout", type=int, default=1800)
    s.add_argument("--full", action="store_true", help="copy the volumes in full too")
    s.set_defaults(fn=cmd_recover)
    s = sub.add_parser("cleanup"); s.add_argument("name")
    s.add_argument("--delete-rollback-volumes", action="store_true")
    s.add_argument("--delete-instance", action="store_true", help="delete the receiver/instance and its volumes")
    s.add_argument("--rename", metavar="NEW", help="rename the migrated instance (hand-over)")
    s.add_argument("--delete-elastic-ips", action="store_true",
                   help="release the Elastic IP exo-v2v reserved (kept by default: it is the production address)")
    s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_cleanup)
    s = sub.add_parser("list"); s.set_defaults(fn=lambda a: [print(n) for n in State.all()])
    from . import group, rehearsal, web
    group.add_parser(sub)
    rehearsal.add_parser(sub)
    web.add_parser(sub)
    service.add_parser(sub)
    alerts.add_parser(sub)
    wan.add_parser(sub)
    from . import jobs, mcp
    jobs.add_parser(sub)
    mcp.add_parser(sub)

    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except (ExoError, FileNotFoundError, FileExistsError, BadName) as e:
        die(str(e))


if __name__ == "__main__":
    main()
