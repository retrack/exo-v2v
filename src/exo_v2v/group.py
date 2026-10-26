"""Application groups: several VMs replicated together and moved as one unit,
with a coordinated cutover (stop front to back, convert in parallel, start
back to front with health checks between the tiers) and an ordered rollback.

  exo-v2v group discover NAME --vm A --vm B ... --zone Z [-o NAME.yaml]
  exo-v2v group plan     NAME -f NAME.yaml
  exo-v2v group create   NAME
  exo-v2v group sync     NAME [--every S] [--cycles N]
  exo-v2v group cutover  NAME [--verify] [--no-warm-cycle]
  exo-v2v group status   NAME
  exo-v2v group rollback NAME
  exo-v2v group cleanup  NAME [--delete-instances]

Members are ordinary migrations named GROUP-VM (`exo-v2v status shop-db01`);
their sync/cutover run as child processes (own vSphere session, lock and log:
~/.exo-v2v/groups/NAME/logs/VM.log). Group state: ~/.exo-v2v/groups/NAME/group.json.
Tiers: 1 = the back end (started first, stopped last), higher = closer to the users.
"""
import argparse
import concurrent.futures as cf
import ipaddress
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

from . import plan as planmod
from .exo import Exo, ExoError
from .state import State, home
from .util import die, human, log, log_prefix, write_json_atomic

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
VM_KEYS = {"vm", "tier", "security_groups", "health", "ip", "name", "instance_type", "system_disk",
           "receiver_template", "receiver_template_visibility", "fold", "no_fold"}
REHEARSAL_KEYS = {"access", "ssh_keys", "front_cidr", "hosts", "ttl"}
DEFAULTS = {"cutover": {"parallel": 8, "max_window": None, "rollback_on_failure": False, "verify": False,
                        "health_timeout": "10m", "boot_timeout": "15m", "max_sync_age": "6h"},
            "sync": {"parallel": 4}}


class GroupFailure(Exception):
    pass


# ---------------- manifest ----------------

def duration(v, default=None):
    """'30m' / '2h' / '90s' / 120 -> seconds."""
    if v in (None, ""):
        return default
    if isinstance(v, (int, float)):
        return int(v)
    m = re.fullmatch(r"\s*(\d+)\s*([smh]?)\s*", str(v))
    if not m:
        raise ValueError(f"bad duration {v!r} (e.g. 90s, 30m, 2h)")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def slug(s):
    return re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")


def member_name(group, vm):
    return f"{group}-{slug(vm)}"


def load_manifest(path):
    import yaml
    try:
        m = yaml.safe_load(open(path))
    except (OSError, yaml.YAMLError) as e:
        die(f"manifest {path}: {e}")
    return validate(m, path)


def validate(m, where="manifest"):
    """Normalized manifest (defaults filled in); dies on the first problem."""
    def bad(msg):
        die(f"{where}: {msg}")
    if not isinstance(m, dict):
        bad("not a mapping")
    for k in ("name", "zone", "vms"):
        if not m.get(k):
            bad(f"`{k}` is required")
    if not NAME_RE.match(m["name"]) or m["name"] == "groups":
        bad(f"name {m['name']!r}: lower-case letters, digits and dashes")
    nets = m.setdefault("networks", {}) or {}
    m["networks"] = nets
    for name, n in nets.items():
        if not isinstance(n, dict) or n.get("type") not in ("public", "private") or not n.get("source"):
            bad(f"network {name}: needs `source` (vSphere port group) and `type` (public|private)")
        if n["type"] == "private":
            try:
                ipaddress.ip_network(n.get("cidr", ""), strict=False)
            except ValueError:
                bad(f"network {name}: `cidr` (e.g. 10.20.0.0/24) is required for a private network")
            n.setdefault("keep_addresses", True)
            n.setdefault("exoscale_name", f"v2v-{m['name']}-{slug(name)}")
    if len({n["source"] for n in nets.values()}) != len(nets):
        bad("two networks have the same `source` port group")
    if not isinstance(m["vms"], list):
        bad("`vms` must be a list")
    seen = set()
    for v in m["vms"]:
        if not isinstance(v, dict) or not v.get("vm"):
            bad("every entry of `vms` needs `vm` (the vSphere VM name)")
        unknown = set(v) - VM_KEYS
        if unknown:
            bad(f"vm {v['vm']}: unknown keys {sorted(unknown)}")
        if v["vm"] in seen:
            bad(f"vm {v['vm']} listed twice")
        seen.add(v["vm"])
        v.setdefault("tier", 1)
        if not isinstance(v["tier"], int) or v["tier"] < 1:
            bad(f"vm {v['vm']}: `tier` is a number >= 1 (1 = started first, stopped last)")
        for k in (m.get("defaults") or {}):
            if k not in VM_KEYS:
                bad(f"defaults: unknown key {k}")
            v.setdefault(k, m["defaults"][k])
        v.setdefault("security_groups", [])
        h = v.get("health") or {}
        if set(h) - {"tcp", "http", "expect", "command", "timeout"}:
            bad(f"vm {v['vm']}: health takes tcp, http, expect, command, timeout")
        for net in (v.get("ip") or {}):
            if net not in nets:
                bad(f"vm {v['vm']}: ip for unknown network {net}")
    aag = m.setdefault("anti_affinity", {}) or {}
    m["anti_affinity"] = aag
    for name, vms in aag.items():
        if not isinstance(vms, list) or set(vms) - seen:
            bad(f"anti_affinity {name}: a list of the group's VMs")
    sgs = m.setdefault("security_groups", {}) or {}
    m["security_groups"] = sgs
    for name, rules in sgs.items():
        for r in rules or []:
            parse_rule(r, bad)
    m["hooks"] = m.get("hooks") or {}
    m["rehearsal"] = m.get("rehearsal") or {}
    if set(m["rehearsal"]) - REHEARSAL_KEYS:
        bad(f"rehearsal takes {', '.join(sorted(REHEARSAL_KEYS))}")
    try:
        for c in m["rehearsal"].get("access") or []:
            ipaddress.ip_network(c, strict=False)
        if m["rehearsal"].get("front_cidr"):
            ipaddress.ip_network(m["rehearsal"]["front_cidr"], strict=False)
        duration(m["rehearsal"].get("ttl"))
    except ValueError as e:
        bad(f"rehearsal: {e}")
    for k in ("cutover", "sync"):
        m[k] = {**DEFAULTS[k], **(m.get(k) or {})}
    for k in ("max_window", "health_timeout", "boot_timeout", "max_sync_age"):
        try:
            m["cutover"][k] = duration(m["cutover"][k])
        except ValueError as e:
            bad(f"cutover.{k}: {e}")
    return m


def parse_rule(r, bad=die):
    """'tcp/80 0.0.0.0/0' | 'udp/53 10.0.0.0/8' | 'icmp 0.0.0.0/0' -> (proto, port, cidr)."""
    parts = str(r).split()
    if len(parts) != 2:
        bad(f"security group rule {r!r}: 'tcp/PORT CIDR' (or udp/PORT, icmp)")
    proto, _, port = parts[0].partition("/")
    if proto not in ("tcp", "udp", "icmp") or (proto != "icmp" and not re.fullmatch(r"\d+(-\d+)?", port)):
        bad(f"security group rule {r!r}: 'tcp/PORT CIDR' (or udp/PORT, icmp)")
    try:
        ipaddress.ip_network(parts[1], strict=False)
    except ValueError:
        bad(f"security group rule {r!r}: bad CIDR")
    return proto, port, parts[1]


# ---------------- group state ----------------

def groups_dir():
    return os.path.join(home(), "groups")


class Group:
    def __init__(self, name):
        self.name = name
        self.dir = os.path.join(groups_dir(), name)
        self.path = os.path.join(self.dir, "group.json")
        self.data = {}

    @classmethod
    def load(cls, name):
        g = cls(name)
        if not os.path.exists(g.path):
            die(f"no group {name!r} (looked in {g.path}); `exo-v2v group plan {name} -f MANIFEST` first")
        g.data = json.load(open(g.path))
        return g

    @staticmethod
    def all():
        d = groups_dir()
        return sorted(x for x in os.listdir(d) if os.path.exists(os.path.join(d, x, "group.json"))) \
            if os.path.isdir(d) else []

    def save(self):
        os.makedirs(os.path.join(self.dir, "logs"), mode=0o700, exist_ok=True)
        write_json_atomic(self.path, self.data)

    def event(self, msg):
        self.data.setdefault("events", []).append({"t": time.time(), "msg": msg})
        self.save()
        log(msg)

    @property
    def m(self):
        return self.data["manifest"]

    def members(self):
        return self.data["members"]

    def tiers(self):
        return sorted({x["tier"] for x in self.members()})

    def of_tier(self, tier):
        return [x for x in self.members() if x["tier"] == tier]

    def lock(self, what):
        """One mutating group command at a time (pid recorded for the web page)."""
        import fcntl
        lk = open(os.path.join(self.dir, ".lock"), "a+")
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            die(f"another exo-v2v group command on {self.name} is running")
        lk.seek(0); lk.truncate()
        lk.write(json.dumps({"pid": os.getpid(), "what": what, "since": time.time()}))
        lk.flush()
        self._lock = lk


def lock_holder(path):
    """{"pid", "what", "since"} of a live holder of a lock file, else None (read only)."""
    try:
        info = json.load(open(path))
        os.kill(int(info["pid"]), 0)
        with open(f"/proc/{info['pid']}/cmdline", "rb") as f:
            if b"exo" not in f.read():
                return None                  # pid reused by something else
        return info
    except (OSError, ValueError, KeyError, TypeError):
        return None


def member_busy(mig):
    return lock_holder(os.path.join(home(), mig, ".lock"))


# ---------------- child processes ----------------

def base_cmd(a):
    cmd = [sys.executable, "-m", "exo_v2v"]
    if a.account:
        cmd += ["--account", a.account]
    if a.vsphere_host:
        cmd += ["--vsphere-host", a.vsphere_host]
    if a.vsphere_user:
        cmd += ["--vsphere-user", a.vsphere_user]
    if a.vsphere_verify_tls:
        cmd.append("--vsphere-verify-tls")
    return cmd


class Child:
    """`exo-v2v CMD MEMBER ...` in a child process writing to the member's log
    (its own session and file: it carries on if this process dies, and a
    group command run again finds it by its lock). Followed here with a [vm]
    prefix."""
    out_lock = threading.Lock()

    def __init__(self, g, a, member, args):
        self.member, self.args = member, args
        path = os.path.join(g.dir, "logs", f"{member['vm']}.log")
        logf = open(path, "a")
        logf.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} exo-v2v {' '.join(args)}\n")
        logf.flush()
        start = logf.tell()
        self.proc = subprocess.Popen(base_cmd(a) + args, stdout=logf, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        logf.close()
        self.tail = []
        self.thread = threading.Thread(target=self._follow, args=(path, start), daemon=True)
        self.thread.start()

    def _follow(self, path, pos):
        with open(path, errors="replace") as f:
            f.seek(pos)
            while True:
                line = f.readline()
                if not line:
                    if self.proc.poll() is not None:
                        rest = f.read()
                        if not rest:
                            return
                        line = rest
                    else:
                        time.sleep(0.5)
                        continue
                for x in line.splitlines():
                    self.tail = (self.tail + [x])[-20:]
                    with Child.out_lock:
                        print(f"[{self.member['vm']}] {x}", file=sys.stderr, flush=True)

    def poll(self):
        return self.proc.poll()

    def wait(self):
        rc = self.proc.wait()
        self.thread.join(10)
        return rc

    def interrupt(self, grace=90):
        """SIGINT first: the member unwinds (snapshot removed, hot-added disks detached)."""
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGINT)
            try:
                self.proc.wait(grace)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(30)

    def error(self):
        self.thread.join(5)
        bad = [x for x in self.tail if x.startswith("error:")]
        return (bad or self.tail or ["(no output)"])[-1]


def run_members(g, a, members, argsfn, parallel):
    """Run one command per member with at most `parallel` at a time.
    Returns {vm: (rc, last error line)}; a failure does not stop the others."""
    res, running, todo = {}, [], list(members)
    while todo or running:
        while todo and len(running) < max(1, parallel):
            x = todo.pop(0)
            running.append(Child(g, a, x, argsfn(x)))
        time.sleep(1)
        for c in list(running):
            if c.poll() is not None:
                running.remove(c)
                rc = c.wait()
                res[c.member["vm"]] = (rc, "" if rc == 0 else c.error())
    return res


# ---------------- commands ----------------

def cmd_discover(a):
    """Draft manifest from vSphere: the VMs' NICs, port groups and addresses."""
    from .cli import vsphere_connect
    V, vc = vsphere_connect(a)
    invs = []
    try:
        for name in a.vm:
            invs.append(V.inventory(vc, vc.vm(name)))
    finally:
        vc.close()
    # Tools rarely say DHCP or static: a port group carrying the default route of a VM
    # with several NICs is its front end (public interface), every other one private
    pgs, front = {}, set()
    for inv in invs:
        for nic in inv["nics"]:
            e = pgs.setdefault(nic["port_group"], {"ips": [], "gw": set()})
            e["ips"] += [ip for ip in nic["ips"] if ip["origin"] != "dhcp"]
            if nic.get("gateway"):
                e["gw"].add(nic["gateway"])
                if len({n["port_group"] for n in inv["nics"]}) > 1:
                    front.add(nic["port_group"])
            if any(ip["origin"] == "dhcp" for ip in nic["ips"]) and not e["ips"]:
                front.add(nic["port_group"])
    L = [f"# exo-v2v group manifest (draft from `group discover`, {time.strftime('%Y-%m-%d')}): check every line.",
         f"name: {a.name}", f"zone: {a.zone}", "",
         "# defaults:                      # any per-VM key, for every VM",
         "#   receiver_template: exo-v2v-linux-legacy", "#   receiver_template_visibility: private", "",
         "networks:                        # vSphere port group -> Exoscale"]
    for pg, e in pgs.items():
        if pg not in front and e["ips"]:
            nets = {ipaddress.ip_network(f"{x['ip']}/{x['prefix']}", strict=False) for x in e["ips"]}
            cidr = str(sorted(nets, key=lambda n: n.prefixlen)[0])
            L += [f"  {slug(pg) or 'net'}:", f"    source: {pg}", "    type: private                # managed private network",
                  f"    cidr: {cidr}", "    keep_addresses: true          # static leases = the VMs' current addresses"]
            if e["gw"]:
                L.append(f"    # on-prem gateway {', '.join(sorted(e['gw']))}: no router on Exoscale, the default "
                         "route moves to the public NIC")
        else:
            L += [f"  {slug(pg) or 'net'}:", f"    source: {pg}",
                  "    type: public                 # the instance's public interface (DHCP)"
                  + ("" if pg in front else "  # (no address seen: check)")]
    L += ["", "# anti_affinity:                  # VMs on different hypervisors (set at creation)",
          "#   app: [app01, app02]", "",
          "# security_groups:                # created by `group create` when missing (names: v2v-...)",
          "#   v2v-" + a.name + "-web: [tcp/80 0.0.0.0/0, tcp/443 0.0.0.0/0]", "",
          "vms:                             # tier 1 = started first, stopped last (databases)"]
    for inv in invs:
        L += [f"  - vm: {inv['name']}", "    tier: 1",
              "    security_groups: []          # existing Exoscale security groups, attached at cutover",
              "    # health: {tcp: 22}          # or {http: \"http://{public_ip}/\", expect: \"text\"} or {command: ...}"]
        for nic in inv["nics"]:
            ips = ", ".join(f"{x['ip']}/{x['prefix']}" + (f" ({x['origin']})" if x["origin"] else "")
                            for x in nic["ips"]) or "no address (Tools?)"
            if nic.get("gateway"):
                ips += f", default route via {nic['gateway']}"
            L.append(f"    # {nic['label']} {nic['mac']} on {nic['port_group']}: {ips}")
    L += ["", "hooks:                           # shell commands on the appliance ({lb01.public_ip} ...)",
          "  # pre_cutover: ./maintenance-page.sh on", "  # post_cutover: ./switch-dns.sh {lb01.public_ip}", "",
          "cutover:", "  parallel: 8                    # member cutovers at a time",
          "  # max_window: 30m              # stop (and roll back, if set below) when exceeded",
          "  rollback_on_failure: false", "  health_timeout: 10m", ""]
    out = a.output or f"{a.name}.yaml"
    if os.path.exists(out) and not a.force:
        die(f"{out} exists (--force to overwrite)")
    open(out, "w").write("\n".join(L))
    print(f"draft manifest: {out} — set the tiers, security groups and health checks, then "
          f"`exo-v2v group plan {a.name} -f {out}`")


def cmd_plan(a):
    from .cli import print_plan, vsphere_connect
    m = load_manifest(a.file)
    if m["name"] != a.name:
        die(f"the manifest is for group {m['name']!r}, not {a.name!r}")
    g = Group(a.name)
    if os.path.exists(g.path):
        old = json.load(open(g.path))
        busy = [x["migration"] for x in old["members"] if os.path.exists(os.path.join(home(), x["migration"]))
                and State.load(x["migration"])["phase"] not in ("planned", "cleaned")]
        if busy:
            die(f"group {a.name} has members past the plan ({', '.join(busy)}): cleanup first")
    V, vc = vsphere_connect(a)
    try:
        invs = {v["vm"]: V.inventory(vc, vc.vm(v["vm"])) for v in m["vms"]}
    finally:
        vc.close()
    ex = Exo(m["zone"], a.account)
    errors, plans = [], {}
    for v in m["vms"]:
        nets, nerr = planmod.network_map(invs[v["vm"]]["nics"], m["networks"], v.get("ip"))
        boot_mode = None
        if v.get("receiver_template"):
            try:
                boot_mode = ex.template_boot_mode(v["receiver_template"], v.get("receiver_template_visibility", "public"))
            except ExoError as e:
                nerr.append(f"receiver template {v['receiver_template']!r}: {e}")
        aags = [f"v2v-{m['name']}-{slug(k)}" for k, vms in m["anti_affinity"].items() if v["vm"] in vms]
        p = planmod.build(invs[v["vm"]], m["zone"], v.get("instance_type"), v.get("system_disk"),
                          v["security_groups"], v.get("receiver_template"),
                          v.get("receiver_template_visibility", "public"), boot_mode,
                          fold=v.get("fold") or [], auto_fold=not v.get("no_fold"),
                          networks=nets, anti_affinity_groups=aags)
        p["errors"] += nerr
        p["inventory"] = invs[v["vm"]]
        p["group"], p["tier"], p["final_name"] = m["name"], v["tier"], v.get("name")
        p["health"] = v.get("health") or {}
        plans[v["vm"]] = p
        errors += [f"{v['vm']}: {e}" for e in p["errors"]]
    errors += group_checks(m, plans, ex)
    for vm, p in plans.items():
        print(f"\n[tier {p['tier']}] ", end="")
        print_plan(p)
        for n in p["networks"]:
            print(f"  NIC {n['source_mac'] or '(new)':<17} -> {n['network']}"
                  + (f" {n['ip']}/{n['prefix']} ({n['mode']})" if n.get("ip") else " (DHCP)"))
    print_runbook(m, plans)
    for e in errors:
        print(f"ERROR: {e}")
    if errors and not a.force:
        die("group plan has errors (fix the manifest or the VMs, or --force to record it anyway)")
    for v in m["vms"]:
        mig = member_name(m["name"], v["vm"])
        st_dir = os.path.join(home(), mig)
        if os.path.exists(os.path.join(st_dir, "state.json")):
            os.remove(os.path.join(st_dir, "state.json"))      # a planned (or cleaned) member: replaced
        st = State.new(mig, plans[v["vm"]])
        st.data["group"] = m["name"]
        st.save()
    g.data = {"name": m["name"], "manifest_file": os.path.abspath(a.file), "manifest": m,
              "created": time.time(), "members": [{"vm": v["vm"], "migration": member_name(m["name"], v["vm"]),
                                                   "tier": v["tier"]} for v in m["vms"]],
              "resources": {}, "events": []}
    g.save()
    g.event(f"group planned: {len(m['vms'])} VMs in {len(g.tiers())} tier(s)")


def group_checks(m, plans, ex):
    errors = []
    seen = {}
    for vm, p in plans.items():
        for n in p["networks"]:
            if n.get("ip"):
                k = (n["network"], n["ip"])
                if k in seen:
                    errors.append(f"{vm} and {seen[k]} both have {n['ip']} on {n['network']}")
                seen[k] = vm
    for sg in {s for p in plans.values() for s in p["security_groups"]}:
        if sg not in m["security_groups"] and not ex.exists("sg", sg):
            errors.append(f"security group {sg} does not exist (create it, or define it under `security_groups:`)")
    for name, vms in m["anti_affinity"].items():
        if len(vms) > 8:
            errors.append(f"anti_affinity {name}: at most 8 instances per anti-affinity group")
    try:
        lim = {x["resource"]: x for x in ex.run("limits")}
        inst = lim.get("Compute instances", {})
        if inst and inst["used"] + len(plans) > inst["max"]:
            errors.append(f"instance quota: {inst['used']} used + {len(plans)} receivers > {inst['max']}")
        pn = lim.get("Private networks", {})
        new = sum(1 for n in m["networks"].values() if n["type"] == "private" and not ex.exists("privnet", n["exoscale_name"]))
        if pn and pn["used"] + new > pn["max"]:
            errors.append(f"private network quota: {pn['used']} used + {new} > {pn['max']}")
    except (ExoError, TypeError, KeyError):
        pass
    return errors


def print_runbook(m, plans):
    tiers = sorted({p["tier"] for p in plans.values()})
    names = lambda t: ", ".join(vm for vm, p in plans.items() if p["tier"] == t)
    print("\ncutover runbook:")
    print("  1. preflight, one last sync cycle on every member" +
          (f", hook pre_cutover: {m['hooks']['pre_cutover']}" if m["hooks"].get("pre_cutover") else ""))
    print("  2. stop, front to back (each VM's final copy and conversion start as soon as it is off):")
    for t in reversed(tiers):
        print(f"       tier {t}: {names(t)}")
    print("  3. conversions in parallel; instances stopped and resized")
    print("  4. start, back to front, health checks between the tiers:")
    for t in tiers:
        print(f"       tier {t}: {names(t)}  " + "; ".join(
            f"{vm} {fmt_health(p['health'])}" for vm, p in plans.items() if p["tier"] == t and p["health"]))
    if m["hooks"].get("post_cutover"):
        print(f"  5. hook post_cutover: {m['hooks']['post_cutover']}")
    c = m["cutover"]
    win = f"{c['max_window'] // 60} min" if c["max_window"] else "none"
    print(f"  max window: {win}, on failure: {'roll back' if c['rollback_on_failure'] else 'stop'}")


def fmt_health(h):
    return " ".join(f"{k}={v}" for k, v in h.items() if k != "timeout")


def cmd_create(a):
    g = Group.load(a.name)
    g.lock("create")
    m, ex = g.m, Exo(g.m["zone"], a.account)
    res = g.data.setdefault("resources", {})
    for name, n in m["networks"].items():
        if n["type"] != "private":
            continue
        pn = n["exoscale_name"]
        if ex.exists("privnet", pn):
            log(f"private network {pn} exists")
        else:
            start, end, mask = planmod.privnet_range(n["cidr"])
            ex.privnet_create(pn, start, end, mask, f"exo-v2v group {m['name']} ({name})")
            res.setdefault("privnets", []).append(pn)
            g.event(f"private network {pn} created ({n['cidr']}, managed {start}-{end})")
    for name in m["anti_affinity"]:
        aag = f"v2v-{m['name']}-{slug(name)}"
        if not ex.exists("aag", aag):
            ex.aag_create(aag)
            res.setdefault("aags", []).append(aag)
            g.event(f"anti-affinity group {aag} created")
    for sg, rules in m["security_groups"].items():
        if not ex.exists("sg", sg):
            ex.sg_create(sg)
            for r in rules or []:
                proto, port, cidr = parse_rule(r)
                args = ["compute", "security-group", "rule", "add", sg, "--network", cidr, "--protocol", proto,
                        "--description", f"exo-v2v group {m['name']}"]
                args += ["--icmp-type", "8", "--icmp-code", "0"] if proto == "icmp" else ["--port", port]
                ex.run(*args, json_out=False)
            res.setdefault("sgs", []).append(sg)
            g.event(f"security group {sg} created ({', '.join(rules or []) or 'no rules'})")
    g.save()
    from .cli import sender_cidr
    cidr = sender_cidr(a)
    todo = [x for x in g.members() if State.load(x["migration"])["phase"] == "planned"]
    for x in g.members():
        if x not in todo:
            log(f"{x['vm']}: receiver already created ({State.load(x['migration'])['phase']})")
    res = run_members(g, a, todo, lambda x: ["create", x["migration"], "--sender-cidr", cidr,
                                             "--timeout", str(a.timeout)], a.parallel)
    bad = {vm: e for vm, (rc, e) in res.items() if rc}
    for vm, (rc, e) in res.items():
        g.event(f"{vm}: receiver {'ready' if not rc else 'FAILED: ' + e}")
    if bad:
        die(f"{len(bad)} receiver(s) failed: {', '.join(bad)} (fix, then `exo-v2v group create {a.name}` again: "
            "ready members are skipped; a failed one may need `exo-v2v cleanup MEMBER --delete-instance` first)")
    print(f"group {a.name}: {len(g.members())} receivers ready; next `exo-v2v group sync {a.name}`")


def cmd_sync(a):
    g = Group.load(a.name)
    g.lock("sync")
    n = 0
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True) or log("stopping after this round"))
    while True:
        t0 = time.time()
        args = ["--full"] if a.full and n == 0 else []
        res = run_members(g, a, g.members(), lambda x: ["sync", x["migration"], "--in-group", *args],
                          a.parallel or g.m["sync"]["parallel"])
        n += 1
        bad = {vm: e for vm, (rc, e) in res.items() if rc}
        g.data["last_sync"] = {"t": time.time(), "seconds": round(time.time() - t0),
                               "failed": {vm: e for vm, e in bad.items()}}
        g.event(f"sync round {n}: {len(res) - len(bad)}/{len(res)} members OK in {round(time.time() - t0)}s"
                + (f"; failed: {', '.join(f'{vm} ({e})' for vm, e in bad.items())}" if bad else ""))
        print_status(g)
        if not a.every or stop["now"] or (a.cycles and n >= a.cycles):
            break
        for _ in range(a.every):
            if stop["now"]:
                break
            time.sleep(1)
        if stop["now"]:
            break
    if g.data["last_sync"]["failed"]:
        sys.exit(1)


# ---------------- coordinated cutover ----------------

def ctx_of(g):
    """Placeholder values: {public_ip}, {private_ip}, {ip.NET} per member and {VM.public_ip} ..."""
    out = {}
    for x in g.members():
        st = State.load(x["migration"])
        r, p = st["receiver"], st["plan"]
        privs = {n["name"]: n["ip"] for n in p.get("networks", []) if n.get("ip")}
        d = {"public_ip": r.get("ip", ""), "private_ip": next(iter(privs.values()), ""),
             "name": r.get("name", ""), "vm": x["vm"], **{f"ip.{k}": v for k, v in privs.items()}}
        out[x["vm"]] = d
    return out


def expand(text, ctx, vm=None):
    def sub(mo):
        k = mo.group(1)
        if vm and k in ctx.get(vm, {}):
            return ctx[vm][k]
        head, _, rest = k.partition(".")
        if head in ctx and rest in ctx[head]:
            return ctx[head][rest]
        return mo.group(0)
    return re.sub(r"\{([A-Za-z0-9_.-]+)\}", sub, text)


def hook_env(g, ctx, step):
    env = dict(os.environ, V2V_GROUP=g.name, V2V_STEP=step)
    for vm, d in ctx.items():
        k = re.sub(r"[^A-Z0-9]", "_", vm.upper())
        env[f"V2V_{k}_PUBLIC_IP"] = d["public_ip"]
        env[f"V2V_{k}_PRIVATE_IP"] = d["private_ip"]
    return env


def run_hook(g, which, ctx):
    cmd = g.m["hooks"].get(which)
    if not cmd:
        return
    cmd = expand(cmd, ctx)
    log(f"hook {which}: {cmd}")
    cwd = os.path.dirname(g.data.get("manifest_file") or ".") or "."
    r = subprocess.run(["bash", "-c", cmd], cwd=cwd, env=hook_env(g, ctx, which), capture_output=True,
                       text=True, timeout=1800)
    for line in (r.stdout + r.stderr).splitlines()[-20:]:
        log(f"  {which}: {line}")
    if r.returncode:
        raise GroupFailure(f"hook {which} failed (rc={r.returncode})")


def health_ports(h):
    """Ports a check reaches on the member's public IP (opened to the appliance)."""
    from urllib.parse import urlparse
    ports = [int(h["tcp"])] if h.get("tcp") else []
    if h.get("http") and "{public_ip}" in h["http"]:
        u = urlparse(h["http"].replace("{public_ip}", "192.0.2.1"))
        ports.append(u.port or (443 if u.scheme == "https" else 80))
    return ports


def health_check(h, ctx, vm, timeout):
    """Retry until the check passes or `timeout`. Returns (ok, detail)."""
    t0 = time.time()
    last = ""
    while True:
        try:
            if h.get("tcp"):
                with socket.create_connection((ctx[vm]["public_ip"], int(h["tcp"])), timeout=5):
                    pass
                last = f"tcp/{h['tcp']} open"
            if h.get("http"):
                url = expand(h["http"], ctx, vm)
                with urllib.request.urlopen(url, timeout=10) as r:
                    body = r.read(65536).decode(errors="replace")
                if h.get("expect") and expand(str(h["expect"]), ctx, vm) not in body:
                    raise OSError(f"{url}: {h['expect']!r} not in the response")
                last = f"{url} -> {r.status}"
            if h.get("command"):
                c = expand(h["command"], ctx, vm)
                p = subprocess.run(["bash", "-c", c], capture_output=True, text=True, timeout=120)
                if p.returncode:
                    raise OSError(f"`{c}` rc={p.returncode}: {(p.stderr or p.stdout).strip()[-200:]}")
                last = f"`{c}` ok"
            return True, last or "no check"
        except (OSError, subprocess.TimeoutExpired, ValueError) as e:
            last = str(e)
        if time.time() - t0 > timeout:
            return False, last
        time.sleep(5)


def preflight(g, a, sts):
    m, problems = g.m, []
    ex = Exo(m["zone"], a.account)
    for x in g.members():
        st = sts[x["vm"]]
        if st["phase"] not in ("syncing", "receiver-ready"):
            problems.append(f"{x['vm']}: phase {st['phase']} (needs a synced receiver)")
            continue
        if not st["cycles"]:
            problems.append(f"{x['vm']}: never synced (`exo-v2v group sync {g.name}`)")
        elif time.time() - st["cycles"][-1]["t"] > m["cutover"]["max_sync_age"]:
            problems.append(f"{x['vm']}: last cycle {human_age(st['cycles'][-1]['t'])} ago "
                            f"(> cutover.max_sync_age): sync first")
        busy = member_busy(x["migration"])
        if busy:
            problems.append(f"{x['vm']}: a {busy['what']} is running (pid {busy['pid']})")
        if st["plan"].get("networks") and not st["receiver"].get("nics"):
            from .cli import attach_networks
            attach_networks(st, ex)        # public-only members created before 0.4.1 never recorded their NICs
        have = {n["network"] for n in st["receiver"].get("nics", [])}
        miss = [n["network"] for n in st["plan"].get("networks", []) if n["network"] not in have]
        if miss:
            problems.append(f"{x['vm']}: receiver lacks NIC(s) on {', '.join(miss)}")
        pf = st.data.get("preflight") or {}
        for export, r in (pf.get("disks") or {}).items():
            for prob in r.get("problems", []):
                if any(k in prob for k in ("dynamic disk", "Storage Spaces", "fold the volumes")):
                    problems.append(f"{x['vm']} {export}: {prob}")
    for x in g.members():
        r = sts[x["vm"]]["receiver"]
        if r.get("name"):
            try:
                if ex.instance(r["name"]).get("state") != "running":
                    problems.append(f"{x['vm']}: receiver {r['name']} is not running")
            except ExoError as e:
                problems.append(f"{x['vm']}: receiver {r.get('name')}: {e}")
    return problems


def human_age(t):
    s = int(time.time() - t)
    return f"{s}s" if s < 120 else f"{s // 60}m" if s < 7200 else f"{s // 3600}h"


def cmd_cutover(a):
    from .cli import exo_for, start_instance, stop_and_scale
    g = Group.load(a.name)
    from .rehearsal import active_for
    busy = sorted({active_for(x["migration"]) for x in g.members()} - {None})
    if busy and not a.allow_rehearsal:
        die(f"rehearsal {', '.join(busy)} is up: `exo-v2v rehearsal destroy {busy[0]}` first, or --allow-rehearsal")
    g.lock("cutover")
    m = g.m
    cut = g.data.get("cutover") or {}
    if cut.get("state") == "done":
        print(f"group {a.name}: cutover already done ({time.ctime(cut['ended'])})")
        return
    if cut.get("state") in (None, "rolled-back", "aborted"):
        cut = {"started": time.time(), "steps": {}, "members": {}}
    cut["state"] = "running"
    g.data["cutover"] = cut
    g.save()
    steps = cut["steps"]
    sts = {x["vm"]: State.load(x["migration"]) for x in g.members()}
    children = {}
    c = m["cutover"]
    verify = a.verify or c["verify"]

    def window_left():
        if c["max_window"] and steps.get("stop_started"):
            left = c["max_window"] - (time.time() - steps["stop_started"])
            if left < 0:
                raise GroupFailure(f"max_window ({c['max_window']}s) exceeded")
            return left

    def mark(step, **kw):
        steps[step] = time.time()
        cut["step"] = step
        cut.update(kw)
        g.save()

    try:
        if "stop_started" not in steps:
            problems = preflight(g, a, sts)
            if problems:
                cut["state"] = "aborted"
                g.save()
                die("preflight:\n  " + "\n  ".join(problems))
            mark("preflight")
            if not a.no_warm_cycle and "warm" not in steps:
                g.event("last warm cycle on every member")
                res = run_members(g, a, g.members(), lambda x: ["sync", x["migration"], "--in-group"],
                                  m["sync"]["parallel"])
                bad = {vm: e for vm, (rc, e) in res.items() if rc}
                if bad:
                    raise GroupFailure(f"warm cycle failed on {', '.join(f'{k} ({v})' for k, v in bad.items())}"
                                       " — nothing was stopped")
                mark("warm")
            if "egress_open" not in steps:
                # before anything stops: a member that cannot get its egress back fails here
                from .cli import open_egress
                ex = Exo(m["zone"], a.account)
                for x in g.members():
                    with log_prefix(x["vm"]):
                        open_egress(State.load(x["migration"]), ex)
                mark("egress_open")
            ctx = ctx_of(g)
            if "pre_hook" not in steps:
                run_hook(g, "pre_cutover", ctx)
                mark("pre_hook")
            mark("stop_started")
            g.event(f"cutover: stopping, front to back (tiers {', '.join(map(str, reversed(g.tiers())))})")

        # stop front to back; each member's final cycle + conversion (child process)
        # starts with its shutdown, the next tier is shut down once this one is off
        for tier in reversed(g.tiers()):
            for x in g.of_tier(tier):
                st = State.load(x["migration"])
                if st["phase"] in ("converted", "done"):
                    continue
                if member_busy(x["migration"]):
                    log(f"{x['vm']}: its cutover is still running (an earlier group cutover): waiting for it")
                    children[x["vm"]] = None
                    continue
                args = ["cutover", x["migration"], "--in-group", "--no-start"] + (["--verify"] if verify else [])
                children[x["vm"]] = Child(g, a, x, args)
            while True:
                window_left()
                off, failed = [], []
                for x in g.of_tier(tier):
                    st = State.load(x["migration"])
                    ch = children.get(x["vm"])
                    if st.data.get("cutover_steps", {}).get("source_off") or st["phase"] in ("converted", "done"):
                        off.append(x["vm"])
                    elif ch is not None and ch.poll() is not None:
                        failed.append(f"{x['vm']} ({ch.error()})")
                    elif ch is None and not member_busy(x["migration"]):
                        failed.append(f"{x['vm']} (its earlier cutover ended before the VM was off)")
                if failed:
                    raise GroupFailure(f"tier {tier}: stopping failed on {', '.join(failed)}")
                if len(off) == len(g.of_tier(tier)):
                    break
                time.sleep(3)
            if f"tier{tier}_off" not in steps:
                mark(f"tier{tier}_off")
                g.event(f"tier {tier} off ({', '.join(off)})")

        # every member converting: wait for all of them
        while True:
            window_left()
            pending, failed = [], []
            for x in g.members():
                st = State.load(x["migration"])
                ch = children.get(x["vm"])
                if st["phase"] in ("converted", "done"):
                    continue
                if ch is not None and ch.poll() is not None:
                    failed.append(f"{x['vm']} ({ch.error()})")
                elif ch is None and not member_busy(x["migration"]):
                    failed.append(f"{x['vm']} (its cutover ended before the conversion finished)")
                else:
                    pending.append(x["vm"])
            if failed:
                raise GroupFailure(f"conversion failed on {', '.join(failed)}")
            if not pending:
                break
            time.sleep(5)
        if "converted" not in steps:
            mark("converted")
            g.event("every member converted")

        # instances: stop (they still run the receiver's RAM root) and resize, all at once
        sts = {x["vm"]: State.load(x["migration"]) for x in g.members()}
        with cf.ThreadPoolExecutor(8) as pool:
            for f in [pool.submit(stop_and_scale, sts[x["vm"]], exo_for(sts[x["vm"]], a))
                      for x in g.members() if sts[x["vm"]]["phase"] == "converted"]:
                f.result()
        ctx = ctx_of(g)

        # start back to front, health checks between the tiers
        for tier in g.tiers():
            if f"tier{tier}_up" in steps:
                continue
            window_left()
            def up(x):
                st = sts[x["vm"]]
                h = st["plan"].get("health") or {}
                if st["phase"] != "done":
                    start_instance(st, exo_for(st, a), c["boot_timeout"], extra_ports=health_ports(h))
                ok, detail = health_check(h, ctx, x["vm"], h.get("timeout") and duration(h["timeout"])
                                          or c["health_timeout"]) if h else (True, "no health check")
                cut["members"].setdefault(x["vm"], {}).update(health=detail, healthy=ok,
                                                              downtime_s=st.data.get("downtime_s"))
                return x["vm"], ok, detail
            with cf.ThreadPoolExecutor(8) as pool:
                res = list(pool.map(up, g.of_tier(tier)))
            g.save()
            for vm, ok, detail in res:
                log(f"{vm}: {'healthy' if ok else 'UNHEALTHY'}: {detail}")
            bad = [f"{vm} ({d})" for vm, ok, d in res if not ok]
            if bad:
                raise GroupFailure(f"tier {tier}: health check failed on {', '.join(bad)}")
            mark(f"tier{tier}_up")
            g.event(f"tier {tier} up and healthy")
        if "post_hook" not in steps:
            try:
                run_hook(g, "post_cutover", ctx)
            except GroupFailure as e:
                g.event(f"WARNING {e} (the application runs on Exoscale)")
            mark("post_hook")
        cut.update(state="done", ended=time.time(), window_s=round(time.time() - steps["stop_started"]))
        g.save()
        g.event(f"cutover done: window {cut['window_s']}s (first stop -> last tier healthy)")
        print_status(g)
        print(f"next: test the application, then `exo-v2v group cleanup {a.name}` (or `group rollback`)")
    except GroupFailure as e:
        cut.update(state="failed", error=str(e), failed_at=time.time())
        g.save()
        g.event(f"CUTOVER STOPPED: {e}")
        live = [ch for ch in children.values() if ch is not None and ch.poll() is None]
        if c["rollback_on_failure"]:
            for ch in live:
                log(f"{ch.member['vm']}: interrupting its cutover")
                ch.interrupt()
            g.event("rollback_on_failure: rolling the group back")
            do_rollback(g, a)
            die(f"cutover failed and was rolled back: {e}")
        if live:
            log(f"waiting for {len(live)} member cutover(s) still running, so that each member stops in a "
                "consistent state (Ctrl-C leaves them running in the background)")
            for ch in live:
                ch.wait()
        print_status(g)
        die(f"cutover stopped: {e}\n  fix it and run `exo-v2v group cutover {a.name}` again (it resumes), or "
            f"`exo-v2v group rollback {a.name}` (sources back on, database first)")


def do_rollback(g, a):
    from .cli import exo_for, rollback, vsphere_connect
    for x in g.members():
        busy = member_busy(x["migration"])
        if busy:
            die(f"{x['vm']}: a {busy['what']} is still running (pid {busy['pid']}); wait for it or stop it first")
    sts = {x["vm"]: State.load(x["migration"]) for x in g.members()}
    # front first: no traffic reaches a half-stopped back end on Exoscale
    for tier in reversed(g.tiers()):
        for x in g.of_tier(tier):
            st, r = sts[x["vm"]], sts[x["vm"]]["receiver"]
            ex = exo_for(st, a)
            if st.data.get("cutover_steps", {}).get("convert_started") and r.get("name") \
                    and ex.instance_exists(r["name"]) and ex.instance(r["name"]).get("state") != "stopped":
                ex.instance_stop(r["name"])
                log(f"{x['vm']}: Exoscale instance {r['name']} stopped")
    V, vc = vsphere_connect(a)
    try:
        for tier in g.tiers():                   # back end first
            for x in g.of_tier(tier):
                st = sts[x["vm"]]
                if st["phase"] in ("planned", "cleaned", "rolled-back") and \
                        str(vc.vm(st["plan"]["vm"]).runtime.powerState) == "poweredOn":
                    continue
                rollback(st, a, V, vc, wait_tools=getattr(a, "tools_timeout", 600))
            g.event(f"tier {tier} back on VMware")
    finally:
        vc.close()
    cut = g.data.get("cutover") or {}
    cut.update(state="rolled-back", rolled_back=time.time())
    g.data["cutover"] = cut
    g.save()
    g.event("group rolled back: the application runs on VMware again")


def cmd_rollback(a):
    g = Group.load(a.name)
    g.lock("rollback")
    do_rollback(g, a)
    print_status(g)


def cmd_cleanup(a):
    from .cli import cleanup
    g = Group.load(a.name)
    g.lock("cleanup")
    for x in g.members():
        st = State.load(x["migration"])
        if st["phase"] == "cleaned":
            continue
        ns = argparse.Namespace(account=a.account, delete_instance=a.delete_instances, force=a.force,
                                delete_rollback_volumes=a.delete_rollback_volumes, rename=None)
        if st["phase"] == "done" and a.delete_instances and not a.force:
            die(f"{x['vm']} is migrated: refusing to delete its instance without --force")
        cleanup(st, ns)
    ex = Exo(g.m["zone"], a.account)
    res = g.data.get("resources", {})
    if a.delete_instances:      # abandoning the move: what the group created goes too
        for kind, fn in (("aags", ex.aag_delete), ("privnets", ex.privnet_delete), ("sgs", ex.sg_delete)):
            for n in list(res.get(kind, [])):
                try:
                    fn(n)
                    res[kind].remove(n)
                    log(f"deleted {n}")
                except ExoError as e:
                    log(f"kept {n}: {e}")
    else:
        kept = [n for k in ("privnets", "aags", "sgs") for n in res.get(k, [])]
        if kept:
            log(f"kept (the migrated instances use them): {', '.join(kept)}")
    g.data["cleaned"] = time.time()
    g.save()
    g.event("group cleaned up")


# ---------------- status ----------------

def member_row(x):
    st = State.load(x["migration"])
    p, r = st["plan"], st["receiver"]
    last = st["cycles"][-1] if st["cycles"] else None
    priv = " ".join(f"{n['ip']}" for n in p.get("networks", []) if n.get("ip"))
    busy = member_busy(x["migration"])
    return {"vm": x["vm"], "tier": x["tier"], "phase": st["phase"], "busy": busy["what"] if busy else "",
            "last": f"{human_age(last['t'])} ago, {human(sum(d['covered'] for d in last['disks'].values()))} "
                    f"in {last['seconds']}s" if last else "-",
            "public": r.get("ip", "-"), "private": priv or "-",
            "downtime": f"{st['downtime_s']}s" if "downtime_s" in st.data else ""}


def print_status(g):
    cut = g.data.get("cutover") or {}
    print(f"group {g.name} ({g.m['zone']}): {len(g.members())} VMs"
          + (f", cutover {cut['state']}" + (f", window {cut.get('window_s')}s" if cut.get("state") == "done" else
                                            f" at {cut.get('step')}" if cut.get("state") in ("running", "failed") else "")
             if cut else ""))
    if cut.get("error"):
        print(f"  error: {cut['error']}")
    print(f"  {'tier':<4} {'vm':<14} {'phase':<14} {'running':<8} {'last cycle':<30} {'public IP':<16} "
          f"{'private':<14} downtime")
    for tier in g.tiers():
        for x in g.of_tier(tier):
            r = member_row(x)
            print(f"  {r['tier']:<4} {r['vm']:<14} {r['phase']:<14} {r['busy']:<8} {r['last']:<30} "
                  f"{r['public']:<16} {r['private']:<14} {r['downtime']}")


def cmd_status(a):
    names = [a.name] if a.name else Group.all()
    for n in names:
        print_status(Group.load(n))
        from .rehearsal import Rehearsal
        rh = Rehearsal.load(n, must=False)
        if rh.active:
            print(f"  rehearsal: {rh.data.get('state')} at {rh.data.get('step')}, relay "
                  f"{rh.data.get('relay', {}).get('ip', '-')} (`exo-v2v rehearsal status {n}`)")


def cmd_rehearse(a):
    """Airgapped copies of the group's members (see `exo-v2v rehearse --help`)."""
    from . import rehearsal as R
    g = Group.load(a.name)
    members = [{"vm": x["vm"], "migration": x["migration"], "tier": x["tier"],
                "health": State.load(x["migration"])["plan"].get("health") or {}}
               for x in g.members() if not a.vm or x["vm"] in a.vm]
    if not members:
        die(f"no member of {a.name} matches {a.vm}")
    R.rehearse(g.name, members, R.settings_of(g.m.get("rehearsal"), a), g.m["zone"], a)


# ---------------- argparse ----------------

def add_parser(sub):
    gp = sub.add_parser("group", help="several VMs moved as one application (coordinated cutover)",
                        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    gs = gp.add_subparsers(dest="gcmd", required=True)
    s = gs.add_parser("discover", help="draft manifest from vSphere")
    s.add_argument("name"); s.add_argument("--vm", action="append", required=True)
    s.add_argument("--zone", required=True); s.add_argument("-o", "--output")
    s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_discover)
    s = gs.add_parser("plan", help="plan every member from the manifest")
    s.add_argument("name"); s.add_argument("-f", "--file", required=True)
    s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_plan)
    s = gs.add_parser("create", help="networks, anti-affinity groups, then the receivers")
    s.add_argument("name"); s.add_argument("--sender-cidr")
    s.add_argument("--parallel", type=int, default=4); s.add_argument("--timeout", type=int, default=1800)
    s.set_defaults(fn=cmd_create)
    s = gs.add_parser("sync", help="a cycle on every member (parallel)")
    s.add_argument("name"); s.add_argument("--every", type=int); s.add_argument("--cycles", type=int)
    s.add_argument("--parallel", type=int); s.add_argument("--full", action="store_true")
    s.set_defaults(fn=cmd_sync)
    s = gs.add_parser("rehearse", help="start airgapped copies of the members, reachable through a relay")
    s.add_argument("name"); s.add_argument("--vm", action="append", help="only these members (repeatable)")
    from .rehearsal import add_options
    add_options(s)
    s.set_defaults(fn=cmd_rehearse)
    s = gs.add_parser("cutover", help="the coordinated cutover (resumable)")
    s.add_argument("--allow-rehearsal", action="store_true", help="cut over although a rehearsal is up")
    s.add_argument("name"); s.add_argument("--verify", action="store_true")
    s.add_argument("--no-warm-cycle", action="store_true"); s.set_defaults(fn=cmd_cutover)
    s = gs.add_parser("status"); s.add_argument("name", nargs="?"); s.set_defaults(fn=cmd_status)
    s = gs.add_parser("rollback", help="sources back on VMware, back end first")
    s.add_argument("name"); s.add_argument("--tools-timeout", type=int, default=600)
    s.set_defaults(fn=cmd_rollback)
    s = gs.add_parser("cleanup", help="member cleanups (rename to final names); --delete-instances abandons")
    s.add_argument("name"); s.add_argument("--delete-instances", action="store_true")
    s.add_argument("--delete-rollback-volumes", action="store_true"); s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_cleanup)
